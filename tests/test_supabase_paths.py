"""
Exercises the Supabase code paths (keyset paging, bulk inserts, RPC balances, undo, rollback) against a tiny
in-process fake of PostgREST. No network, no real database, dummy URL/key.
"""
import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.text = data, status, json.dumps(data)

    def json(self, **kw):
        return json.loads(self.text, **kw)


class FakePostgrest:
    def __init__(self, rpc=True):
        self.t = {n: [] for n in ("transactions", "pool_contributions", "pool_expenses", "settings", "aliases", "chat_log")}
        self.seq, self.calls, self.rpc, self.fail_bulk = 0, [], rpc, False

    def _flt(self, rows, params):
        for k, v in params.items():
            if k in ("select", "order", "limit", "offset", "on_conflict"):
                continue
            op, _, val = v.partition(".")
            if v == "is.null":
                rows = [r for r in rows if r.get(k) is None]
            elif op == "in":
                ids = val.strip("()").split(",")
                rows = [r for r in rows if str(r.get(k)) in ids]
            elif op == "like":
                rows = [r for r in rows if str(r.get(k, "")).startswith(val.rstrip("*"))]
            else:
                cmp = {"eq": lambda a, b: str(a) == b, "neq": lambda a, b: str(a) != b,
                       "gt": lambda a, b: float(a) > float(b), "lt": lambda a, b: float(a) < float(b),
                       "gte": lambda a, b: str(a) >= b}[op]
                rows = [r for r in rows if cmp(r.get(k), val)]
        return rows

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        path = url.split("/rest/v1/")[1]
        params = dict(params or {})
        self.calls.append((method, path, dict(params)))
        prefer = (headers or {}).get("Prefer", "")
        if path == "rpc/bucket_balances":
            if not self.rpc:
                return Resp({"code": "PGRST202"}, 404)
            tot = {}
            for r in self.t["transactions"]:
                a = float(r["amount"])
                if r["type"] == "transfer":
                    tot[r.get("from_bucket") or "cash"] = tot.get(r.get("from_bucket") or "cash", 0) - a
                    tot[r.get("to_bucket") or "cash"] = tot.get(r.get("to_bucket") or "cash", 0) + a
                else:
                    b = r.get("payment_method") or "cash"
                    tot[b] = tot.get(b, 0) + (a if r["type"] == "income" else -a)
            return Resp([{"bucket": b, "total": v} for b, v in tot.items()])
        rows = self.t[path]
        if method == "GET":
            out = self._flt(list(rows), params)
            if "order" in params:
                col, _, d = params["order"].partition(".")
                out.sort(key=lambda r: (r.get(col) is None, r.get(col)), reverse=(d == "desc"))
            off, lim = int(params.get("offset", 0)), int(params.get("limit", 10 ** 9))
            return Resp(out[off:off + lim])
        if method == "POST":
            items = json if isinstance(json, list) else [json]
            if self.fail_bulk and isinstance(json, list):
                return Resp({"message": "boom"}, 500)
            made = []
            for it in items:
                it = dict(it)
                if "on_conflict" in params:
                    key = params["on_conflict"]
                    rows[:] = [r for r in rows if r.get(key) != it.get(key)]
                else:
                    self.seq += 1
                    it["id"] = self.seq
                    it["created_at"] = (datetime(2026, 11, 8, tzinfo=timezone.utc) + timedelta(seconds=self.seq)).isoformat()
                if "amount" in it:
                    it["amount"] = float(it["amount"])
                rows.append(it)
                made.append(it)
            return Resp(made if "representation" in prefer else [], 201)
        if method in ("PATCH", "DELETE"):
            hit = self._flt(list(rows), params)
            if method == "DELETE":
                rows[:] = [r for r in rows if r not in hit]
            else:
                for r in hit:
                    r.update(json)
            return Resp(hit if "representation" in prefer else [])
        raise AssertionError(method)


@pytest.fixture()
def sb(monkeypatch):
    for k in ("GROQ_API_KEY", "SESSION_SECRET", "VERCEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FOGLESS_NO_DOTENV", "1")
    monkeypatch.setenv("SUPABASE_URL", "http://fake.invalid")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "dummy")
    monkeypatch.setenv("DEMO_USERNAME", "u")
    monkeypatch.setenv("DEMO_PASSWORD", "p")
    spec = importlib.util.spec_from_file_location("fog_sb", os.path.join(ROOT, "api", "index.py"))
    fog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fog)
    fog.SB_PAGE_SIZE = 3
    fake = FakePostgrest()
    monkeypatch.setattr(fog.requests, "request", fake.request)
    monkeypatch.setattr(fog.requests, "post", lambda url, **kw: fake.request("POST", url, kw.get("headers"), None, kw.get("json")))
    monkeypatch.setattr(fog.requests, "get", lambda url, **kw: fake.request("GET", url, kw.get("headers"), kw.get("params")))
    cl = fog.app.test_client()
    assert cl.post("/api/login", json={"username": "u", "password": "p"}).status_code == 200
    return fog, fake, cl


def say(cl, t):
    return cl.post("/api/message", json={"text": t}).get_json()


def test_keyset_paging_returns_every_row_once(sb):
    fog, fake, cl = sb
    for i in range(1, 9):
        fog.add_transaction("expense", i, "food", f"n{i}", "cash")
    ids = [r["id"] for r in fog._all_transactions()]
    assert ids == list(range(8, 0, -1))
    assert [r["id"] for r in fog._all_transactions(order="id.asc")] == list(range(1, 9))
    assert len(fog._all_transactions(limit=5)) == 5
    assert all("offset" not in c[2] for c in fake.calls if c[0] == "GET" and c[1] == "transactions")


def test_rpc_balances_match_slow_path(sb):
    fog, fake, cl = sb
    say(cl, "/setbalance cash 1000")
    say(cl, "gave rahul 200 cash")
    say(cl, "500 dinner upi split with amit")
    fast = fog.get_balances_by_method()
    fake.rpc, fog._RPC["ok"] = False, None
    slow = fog.get_balances_by_method()
    assert {k: v for k, v in fast.items() if v} == {k: v for k, v in slow.items() if v}
    assert fog._RPC["ok"] is False  # remembered: no more doomed RPC calls


def test_split_is_two_requests_and_rolls_back(sb):
    fog, fake, cl = sb
    say(cl, "100 dinner split with a and b and c")
    posts = [c for c in fake.calls if c[0] == "POST" and c[1] == "transactions"]
    assert len(posts) == 2  # my share + ONE bulk insert of 3 IOUs (was 4 sequential writes)
    fake.t["transactions"].clear()
    fake.fail_bulk = True
    r = cl.post("/api/message", json={"text": "100 dinner split with a and b"})
    assert r.status_code == 503 and fake.t["transactions"] == []  # my-share expense was rolled back


def test_multi_item_is_one_bulk_insert_ids_in_order(sb):
    fog, fake, cl = sb
    say(cl, "120 lunch upi, 50 bus cash, 200 veggies card")
    posts = [c for c in fake.calls if c[0] == "POST" and c[1] == "transactions"]
    assert len(posts) == 1
    assert [r["amount"] for r in fake.t["transactions"]] == [120.0, 50.0, 200.0]


def test_undo_newest_across_tables(sb):
    fog, fake, cl = sb
    say(cl, "put 500 into goa pool")
    say(cl, "300 from goa pool")
    assert "Reverted" in say(cl, "/undo")["text"]
    assert fake.t["pool_expenses"] == [] and len(fake.t["pool_contributions"]) == 1
    say(cl, "/undo")
    assert fake.t["pool_contributions"] == [] and fake.t["transactions"] == []


def test_today_spend_only_downloads_expenses(sb):
    fog, fake, cl = sb
    say(cl, "100 lunch")
    fake.calls.clear()
    fog.get_today_expense()
    assert any(c[2].get("type") == "eq.expense" for c in fake.calls)
