"""A small in-process stand-in for Supabase, just faithful enough to test Fogless end to end.

Auth (GoTrue):  signup, token (password / refresh_token), user (GET/PUT), logout, recover, admin delete
Data (PostgREST): GET/POST/PATCH/DELETE with eq/neq/gt/gte/lt/lte/in/like filters, order/limit/offset,
                  Prefer: return=representation|minimal, resolution=merge-duplicates, count=exact,
                  and the unique constraints the real schema has (so a wrong on_conflict fails like it would
                  on the real thing).
It also records every request so tests can assert which key was used where.
"""
import json
import re
import secrets
import threading
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qsl

ANON_KEY = "anon-key"
SERVICE_KEY = "service-key"

TABLES = ("transactions", "aliases", "settings", "pool_contributions", "pool_expenses", "chat_log")
HAS_ID = {"transactions", "pool_contributions", "pool_expenses", "chat_log"}
UNIQUE = {"aliases": ("user_id", "note_key"), "settings": ("user_id", "key")}
REQUIRED = {
    "transactions": ("type", "amount"),
    "aliases": ("note_key", "type", "category"),
    "settings": ("key",),
    "pool_contributions": ("pool", "person", "amount"),
    "pool_expenses": ("pool", "amount", "category"),
    "chat_log": ("role", "content"),
}
RESERVED = {"select", "order", "limit", "offset", "on_conflict"}


class FakeSupabase:
    def __init__(self):
        self.users = {}          # id -> dict(id, email, password, meta, confirmed)
        self.access = {}         # access token -> (user_id, session_id)
        self.refresh = {}        # refresh token -> (user_id, session_id)
        self.expired = set()     # access tokens to treat as expired
        self.tables = {t: [] for t in TABLES}
        self.seq = {t: 0 for t in HAS_ID}
        self.confirm_email = False
        self.rpc_installed = True     # False -> bucket_balances 404s like a project that skipped migration 004
        self.fail_bulk_transactions = False  # True -> list POSTs to transactions answer 500
        self.recover_requests = []
        self.calls = []          # (method, path, key_role)
        self.rest_requests = []  # (method, table, {param: value}, request body)
        self.lock = threading.RLock()
        handler = self._make_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------ helpers for tests
    def user_by_email(self, email):
        return next((u for u in self.users.values() if u["email"] == email), None)

    def confirm(self, email):
        self.user_by_email(email)["confirmed"] = True

    def make_recovery_token(self, email):
        u = self.user_by_email(email)
        return self._new_session(u["id"])["access_token"]

    def rows_for(self, table, user_id):
        return [r for r in self.tables[table] if r.get("user_id") == user_id]

    # ------------------------------------------------------------------ auth internals
    def _new_session(self, user_id):
        sid = str(uuid.uuid4())
        at, rt = "at-" + secrets.token_hex(24), "rt-" + secrets.token_hex(24)
        self.access[at] = (user_id, sid)
        self.refresh[rt] = (user_id, sid)
        u = self.users[user_id]
        return {"access_token": at, "refresh_token": rt, "token_type": "bearer", "expires_in": 3600,
                "user": self._user_json(u)}

    @staticmethod
    def _user_json(u):
        return {"id": u["id"], "email": u["email"], "user_metadata": dict(u["meta"])}

    def _drop_user(self, user_id):
        self.users.pop(user_id, None)
        for m in (self.access, self.refresh):
            for k in [k for k, v in m.items() if v[0] == user_id]:
                m.pop(k)
        for t in TABLES:  # ON DELETE CASCADE
            self.tables[t] = [r for r in self.tables[t] if r.get("user_id") != user_id]

    # ------------------------------------------------------------------ HTTP
    def _make_handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body=None, headers=None):
                data = b"" if body is None else json.dumps(body, default=_jd).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return None
                return json.loads(self.rfile.read(n).decode() or "null")

            def _role(self):
                key = self.headers.get("apikey")
                bearer = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                if key == ANON_KEY:
                    return "anon", bearer
                if key == SERVICE_KEY:
                    return "service", bearer
                return None, bearer

            def do_GET(self): self._dispatch()
            def do_POST(self): self._dispatch()
            def do_PUT(self): self._dispatch()
            def do_PATCH(self): self._dispatch()
            def do_DELETE(self): self._dispatch()

            def _dispatch(self):
                with fake.lock:
                    parts = urlsplit(self.path)
                    role, bearer = self._role()
                    fake.calls.append((self.command, parts.path, role))
                    if role is None:
                        return self._send(401, {"message": "Invalid API key"})
                    query = parse_qsl(parts.query, keep_blank_values=True)
                    if parts.path.startswith("/auth/v1/"):
                        return fake._auth(self, parts.path[len("/auth/v1/"):], role, bearer, query)
                    if parts.path.startswith("/rest/v1/"):
                        return fake._rest(self, parts.path[len("/rest/v1/"):], role, bearer, query)
                    return self._send(404, {"message": "not found"})

        return H

    # ------------------------------------------------------------------ GoTrue
    def _bearer_user(self, bearer):
        if bearer in self.expired or bearer not in self.access:
            return None
        return self.users.get(self.access[bearer][0])

    def _auth(self, h, path, role, bearer, query):
        method, q = h.command, dict(query)
        body = h._body() or {}
        if path == "signup" and method == "POST":
            email, pw = (body.get("email") or "").lower(), body.get("password") or ""
            if len(pw) < 6:
                return h._send(422, {"code": 422, "error_code": "weak_password",
                                     "msg": "Password should be at least 6 characters."})
            existing = self.user_by_email(email)
            if existing:
                if self.confirm_email:  # obfuscated like real GoTrue: looks like a fresh signup
                    return h._send(200, {"id": str(uuid.uuid4()), "email": email})
                return h._send(422, {"code": 422, "error_code": "user_already_exists",
                                     "msg": "User already registered"})
            uid = str(uuid.uuid4())
            self.users[uid] = {"id": uid, "email": email, "password": pw, "meta": dict(body.get("data") or {}),
                               "confirmed": not self.confirm_email}
            if self.confirm_email:
                return h._send(200, {"id": uid, "email": email})
            return h._send(200, self._new_session(uid))
        if path == "token" and method == "POST":
            if q.get("grant_type") == "password":
                u = self.user_by_email((body.get("email") or "").lower())
                if not u or u["password"] != body.get("password"):
                    return h._send(400, {"code": 400, "error_code": "invalid_credentials",
                                         "msg": "Invalid login credentials"})
                if not u["confirmed"]:
                    return h._send(400, {"code": 400, "error_code": "email_not_confirmed",
                                         "msg": "Email not confirmed"})
                return h._send(200, self._new_session(u["id"]))
            if q.get("grant_type") == "refresh_token":
                entry = self.refresh.pop(body.get("refresh_token"), None)  # single use, like the real thing
                if not entry or entry[0] not in self.users:
                    return h._send(400, {"code": 400, "error_code": "refresh_token_not_found",
                                         "msg": "Invalid Refresh Token: Refresh Token Not Found"})
                return h._send(200, self._new_session(entry[0]))
        if path == "user" and method == "GET":
            if role != "anon":
                return h._send(401, {"msg": "wrong key"})
            u = self._bearer_user(bearer)
            if not u:
                return h._send(401, {"code": 401, "error_code": "bad_jwt", "msg": "invalid JWT"})
            return h._send(200, self._user_json(u))
        if path == "user" and method == "PUT":
            u = self._bearer_user(bearer)
            if not u:
                return h._send(401, {"code": 401, "error_code": "bad_jwt", "msg": "invalid JWT"})
            if "password" in body:
                if len(body["password"]) < 6:
                    return h._send(422, {"code": 422, "error_code": "weak_password",
                                         "msg": "Password should be at least 6 characters."})
                if body["password"] == u["password"]:
                    return h._send(422, {"code": 422, "error_code": "same_password",
                                         "msg": "New password should be different from the old password."})
                u["password"] = body["password"]
            if isinstance(body.get("data"), dict):
                u["meta"].update(body["data"])
            return h._send(200, self._user_json(u))
        if path == "logout" and method == "POST":
            entry = self.access.get(bearer)
            if not entry:
                return h._send(401, {"error_code": "bad_jwt", "msg": "invalid JWT"})
            sid = entry[1]
            for m in (self.access, self.refresh):
                for k in [k for k, v in m.items() if v[1] == sid]:
                    m.pop(k)
            return h._send(204)
        if path == "recover" and method == "POST":
            self.recover_requests.append((body.get("email"), q.get("redirect_to")))
            return h._send(200, {})
        if path.startswith("admin/users/") and method == "DELETE":
            if role != "service":
                return h._send(403, {"msg": "not_admin"})
            uid = path.rsplit("/", 1)[1]
            if uid not in self.users:
                return h._send(404, {"msg": "User not found"})
            self._drop_user(uid)
            return h._send(200, {})
        return h._send(404, {"msg": f"no such auth route {method} {path}"})

    # ------------------------------------------------------------------ PostgREST
    def _rest(self, h, table, role, bearer, query):
        if role != "service":
            return h._send(401, {"message": "data access requires the service key in this app"})
        if table.startswith("rpc/"):
            return self._rpc(h, table[4:])
        if table not in self.tables:
            return h._send(404, {"code": "42P01", "message": f"relation {table} does not exist"})
        method = h.command
        params = dict(query)
        body = h._body() if method in ("POST", "PATCH") else None
        self.rest_requests.append((method, table, params, body))
        prefer = {p.strip() for p in (h.headers.get("Prefer") or "").split(",") if p.strip()}
        rows = self.tables[table]
        filters = [(k, v) for k, v in query if k not in RESERVED]

        def match(r):
            return all(_match(r.get(col), val) for col, val in filters)

        if method == "GET":
            sel = [r for r in rows if match(r)]
            total = len(sel)
            order = params.get("order")
            if order:
                col, _, d = order.partition(".")
                sel.sort(key=lambda r: _sortkey(r.get(col)), reverse=(d == "desc"))
            off, lim = int(params.get("offset") or 0), params.get("limit")
            sel = sel[off:off + int(lim)] if lim is not None else sel[off:]
            cols = params.get("select", "*")
            if cols != "*":
                keep = [c.strip() for c in cols.split(",")]
                sel = [{k: r.get(k) for k in keep} for r in sel]
            hdrs = {}
            if "count=exact" in prefer:
                hdrs["Content-Range"] = (f"{off}-{off + len(sel) - 1}/{total}" if sel else f"*/{total}")
            return h._send(200, [_out(r) for r in sel], hdrs)

        if method == "POST":
            items = body if isinstance(body, list) else [body]
            if self.fail_bulk_transactions and table == "transactions" and isinstance(body, list):
                return h._send(500, {"message": "simulated outage"})
            conflict = tuple(c.strip() for c in params["on_conflict"].split(",")) if params.get("on_conflict") else None
            if conflict and conflict != UNIQUE.get(table):
                return h._send(400, {"code": "42P10", "message": "there is no unique or exclusion constraint "
                                                                   "matching the ON CONFLICT specification"})
            out = []
            for item in items:
                if not item.get("user_id"):
                    return h._send(400, {"code": "23502", "message": "null value in column user_id"})
                for col in REQUIRED[table]:
                    if item.get(col) is None:
                        return h._send(400, {"code": "23502", "message": f"null value in column {col}"})
                if table == "transactions" and item["type"] not in ("expense", "income", "transfer"):
                    return h._send(400, {"code": "23514", "message": "transactions_type_check"})
                if item["user_id"] not in self.users:
                    return h._send(409, {"code": "23503", "message": "foreign key violation on user_id"})
                existing = None
                if table in UNIQUE:
                    key = UNIQUE[table]
                    existing = next((r for r in rows if all(r.get(k) == item.get(k) for k in key)), None)
                if existing:
                    if "resolution=merge-duplicates" not in prefer:
                        return h._send(409, {"code": "23505", "message": "duplicate key value"})
                    existing.update(item)
                    out.append(existing)
                    continue
                row = dict(item)
                row.setdefault("created_at", datetime.now(timezone.utc).isoformat())
                if table in HAS_ID:
                    self.seq[table] += 1
                    row["id"] = self.seq[table]
                if table == "aliases":
                    row.setdefault("confirmed", False)
                    row.setdefault("candidate_count", 1)
                rows.append(row)
                out.append(row)
            if "return=representation" in prefer:
                return h._send(201, [_out(r) for r in out])
            return h._send(201)

        if method == "PATCH":
            body = body or {}
            hit = [r for r in rows if match(r)]
            for r in hit:
                r.update(body)
            if "return=representation" in prefer:
                return h._send(200, [_out(r) for r in hit])
            return h._send(204)

        if method == "DELETE":
            hit = [r for r in rows if match(r)]
            self.tables[table] = [r for r in rows if r not in hit]
            if "return=representation" in prefer:
                return h._send(200, [_out(r) for r in hit])
            return h._send(204)

        return h._send(405, {"message": "method not allowed"})


    def _rpc(self, h, name):
        """bucket_balances(p_user_id uuid): the same maths as the SQL function in 004 -- one user's rows only."""
        body = h._body() or {}
        self.rest_requests.append(("POST", "rpc/" + name, {}, body))
        if name != "bucket_balances" or not self.rpc_installed or set(body) != {"p_user_id"}:
            return h._send(404, {"code": "PGRST202", "message": "Could not find the function in the schema cache"})
        totals = {}
        for r in self.tables["transactions"]:
            if r["user_id"] != body["p_user_id"]:
                continue
            amt = Decimal(str(r["amount"]))
            if r["type"] == "transfer":
                frm, to = r.get("from_bucket") or "cash", r.get("to_bucket") or "cash"
                totals[frm] = totals.get(frm, 0) - amt
                totals[to] = totals.get(to, 0) + amt
            else:
                b = r.get("payment_method") or "cash"
                totals[b] = totals.get(b, 0) + (amt if r["type"] == "income" else -amt)
        return h._send(200, [{"bucket": b, "total": float(t)} for b, t in totals.items()])


# ---------------------------------------------------------------------- filter + serialisation helpers
def _jd(o):
    if isinstance(o, Decimal):
        return float(o)
    raise TypeError(type(o))


def _out(r):
    r = dict(r)
    if "amount" in r and r["amount"] is not None:
        r["amount"] = float(Decimal(str(r["amount"])))  # PostgREST returns numeric as a JSON number
    return r


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _sortkey(v):
    return (v is not None, v if v is not None else 0)


def _match(value, spec):
    op, _, arg = spec.partition(".")
    if isinstance(value, bool):
        value = "true" if value else "false"
    if op == "is":
        return (value is None) if arg == "null" else (value is (arg == "true"))
    if op == "in":
        return str(value) in [a.strip() for a in arg.strip("()").split(",")]
    if op == "like":
        return re.fullmatch(re.escape(arg).replace(r"\*", ".*"), str(value)) is not None
    if op in ("eq", "neq"):
        same = str(value) == arg
        return same if op == "eq" else not same
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _num(value), _num(arg)
        if a is None or b is None:
            a, b = ("" if value is None else str(value)), arg
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    raise ValueError(f"unsupported filter {spec!r}")
