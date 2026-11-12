"""
apply_fixes.py  --  run ONCE from the project root:   python apply_fixes.py

Patches api/index.py. All-or-nothing: every replacement must match exactly the expected number of
times, the result must compile, otherwise NOTHING is written. CRLF/LF is preserved.
Undo with:  git checkout api/index.py
"""
import re
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "api/index.py"
raw = open(PATH, "rb").read().decode("utf-8")
crlf = "\r\n" in raw
src = raw.replace("\r\n", "\n")

if "def undo_last_action" in src:
    sys.exit("Already patched. Nothing changed.")


def rep(old, new, count=1):
    global src
    n = src.count(old)
    if n != count:
        sys.exit(f"PATCH FAILED: expected {count} match(es), found {n} for:\n{old[:200]}\nNothing was changed.")
    src = src.replace(old, new)


def rep_re(pattern, new, count=1):
    global src
    src, n = re.subn(pattern, new, src, flags=re.S)
    if n != count:
        sys.exit(f"PATCH FAILED: regex expected {count}, found {n}: {pattern[:120]}\nNothing was changed.")


# ============================================================ 1. imports / .env loader / config
rep("from flask import Flask, request, jsonify, send_file, Response, render_template_string, session\n"
    "import hashlib\n",
    "from flask import Flask, request, jsonify, send_file, session\nimport hashlib\nimport secrets\n")

rep('''# --------------------------------------------------------------------- config


CRON_SECRET = os.getenv("CRON_SECRET", "")

# Demo login. Override with DEMO_USERNAME / DEMO_PASSWORD env vars if you ever want different ones.
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "vaibhav10")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "iwon10")
''', '''# --------------------------------------------------------------------- config


def _load_dotenv(path=None):
    """Tiny .env reader (no dependency) so the README's local setup really works. Real environment
    variables always win; it never runs on Vercel or when FOGLESS_NO_DOTENV=1 (used by the tests)."""
    if os.getenv("VERCEL") or os.getenv("FOGLESS_NO_DOTENV"):
        return
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()

# Login credentials come ONLY from the environment. No defaults: if they are missing, login is
# refused (fails closed) instead of silently accepting a password that is public in the repo.
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "").strip()
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "")
''')

rep('''SESSION_SECRET = os.getenv("SESSION_SECRET") or (
    hashlib.sha256(("fogless-session:" + SUPABASE_KEY).encode("utf-8")).hexdigest()
    if SUPABASE_KEY else "fogless-local-demo-secret"
)''', '''SESSION_SECRET = os.getenv("SESSION_SECRET") or (
    hashlib.sha256(("fogless-session:" + SUPABASE_KEY).encode("utf-8")).hexdigest()
    if SUPABASE_KEY else secrets.token_hex(32)  # random per process: never a secret that is public in the repo
)''')

rep('''    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)
''', '''    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # Vercel rejects bigger bodies anyway; stops giant uploads locally
)


@app.after_request
def _security_headers(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"  # balances/ledger must never be cached
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def _json_body() -> dict:
    """request JSON as a dict, always. A list/str/number body used to crash with AttributeError (HTTP 500)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}
''')

rep('app.run(host="0.0.0.0", port=5000, debug=True)',
    '# debug=True + 0.0.0.0 exposes the Werkzeug debugger (remote code execution) to the whole network\n'
    '    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")')

# ============================================================ 2. login: fail closed + throttle
rep('''def _is_logged_in() -> bool:
    return session.get("user") == DEMO_USERNAME
''', '''def _is_logged_in() -> bool:
    return bool(DEMO_USERNAME) and session.get("user") == DEMO_USERNAME


_LOGIN_FAILS = {}  # ip -> (failures, window_start). Best effort: one dict per serverless instance.
_LOGIN_MAX, _LOGIN_WINDOW = 8, 600


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() or request.remote_addr or "?"
''')

rep('''    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
''', '''    if not (DEMO_USERNAME and DEMO_PASSWORD):
        return _no_store(jsonify({"ok": False, "text": "Login is not configured on the server "
                                  "(set DEMO_USERNAME and DEMO_PASSWORD)."})), 503
    ip, now = _client_ip(), time.time()
    fails, started = _LOGIN_FAILS.get(ip, (0, now))
    if now - started > _LOGIN_WINDOW:
        fails, started = 0, now
    if fails >= _LOGIN_MAX:
        return _no_store(jsonify({"ok": False, "text": "Too many attempts. Try again in a few minutes."})), 429
    data = _json_body()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
''')

rep('''    if user_ok and pass_ok:
        session.clear()''', '''    if user_ok and pass_ok:
        _LOGIN_FAILS.pop(ip, None)
        session.clear()''')

rep('''    time.sleep(0.4)  # blunt the guessing speed a little
''', '''    if len(_LOGIN_FAILS) > 1000:
        _LOGIN_FAILS.clear()
    _LOGIN_FAILS[ip] = (fails + 1, started)
    time.sleep(0.4)  # blunt the guessing speed a little
''')

# ============================================================ 3. request-body hardening
rep('''    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:''', '''    data = _json_body()
    raw_text = data.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""  # {"text": 50} / {"text": ["a"]} are not messages
    if not text:''')

rep('''    data = request.get_json(silent=True) or {}
    action = data.get("action")
    tx_id = None
    if data.get("tx_id") is not None:
        try:
            tx_id = int(data.get("tx_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "text": "Invalid transaction id."}), 400
''', '''    data = _json_body()
    action = data.get("action")
    tx_id = None
    raw_id = data.get("tx_id")
    if raw_id is not None:
        # strict: int(1.9) == 1 and int(True) == 1 used to silently target the WRONG row
        ok_type = (isinstance(raw_id, int) and not isinstance(raw_id, bool)) or \
                  (isinstance(raw_id, str) and raw_id.strip().isdigit())
        if not ok_type or not (0 < int(raw_id) < 2 ** 62):
            return jsonify({"ok": False, "text": "Invalid transaction id."}), 400
        tx_id = int(raw_id)
''')

# ============================================================ 4. amount text normalisation ("30₹ chai" -> "30chai")
rep('_SYMBOL_RE = re.compile(r"[₹$]\\s*")', '_SYMBOL_RE = re.compile(r"\\s*[₹$]\\s*")')
rep('t = _SYMBOL_RE.sub("", text or "")', 't = _SYMBOL_RE.sub(" ", text or "")')

# ============================================================ 5. fallback parser fixes
rep('''_INCOME_RE = re.compile(
    r"\\b(?:income|salary|got paid|got|received|earned|payout|refund|credited|deposited)\\b", re.IGNORECASE
)''', '''# bare "got" used to flip "300 got milk" / "got haircut 200" into INCOME. "got" now only counts when an
# amount or "paid" follows it ("got 424 upi", "got paid 5000").
_INCOME_RE = re.compile(
    r"\\b(?:income|salary|got\\s+paid|received|earned|payout|refund|credited|deposited)\\b"
    r"|\\bgot\\s+(?:rs\\.?\\s*)?[-\u2212]?\\d", re.IGNORECASE
)

_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE_RES = [
    re.compile(rf"\\b\\d{{1,2}}(?:st|nd|rd|th)?\\s+{_MON}\\b(?:\\s+\\d{{4}})?", re.I),   # 12 nov, 5th march 2026
    re.compile(rf"\\b{_MON}\\s+\\d{{1,2}}(?:st|nd|rd|th)?\\b(?:,?\\s+\\d{{4}})?", re.I),  # nov 12
    re.compile(r"\\b\\d{1,2}[/-]\\d{1,2}(?:[/-]\\d{2,4})?\\b"),                          # 12/11, 12-11-2026
    re.compile(r"\\b\\d+(?:st|nd|rd|th)\\b", re.I),                                       # 5th
]
_QTY_FOR_RE = re.compile(r"\\b(\\d{1,2})\\s+([a-z][a-z]*(?:\\s[a-z]+){0,2}?)\\s+(?:for|at|@|worth|total)\\s+(\\d+(?:\\.\\d+)?k?)\\b", re.I)
_QTY_ON_RE = re.compile(r"\\b(\\d+(?:\\.\\d+)?k?)\\s+(?:on|for)\\s+(\\d{1,2})\\s+([a-z]{3,})\\b", re.I)


def _fallback_prep(text):
    """Dates and quantities are not amounts: '450 dinner on 12 nov' used to log a phantom Rs12 expense,
    '3 shirts for 1500' logged Rs3, 'spent 20 on 2 samosas' logged two items."""
    t = text
    for rx in _DATE_RES:
        t = rx.sub(" ", t)
    t = _QTY_FOR_RE.sub(lambda m: f"{m.group(3)} {m.group(2)} x{m.group(1)}", t)
    t = _QTY_ON_RE.sub(lambda m: f"{m.group(1)} {m.group(3)} x{m.group(2)}", t)
    return re.sub(r"[ \\t]+", " ", t).strip()''')

rep('''def _fallback_rule_parse(text):
    """Rule-based regex parser when Groq API key is absent or unavailable."""
    debt_item = _fallback_debt_parse(text)''', '''def _fallback_rule_parse(text):
    """Rule-based regex parser when Groq API key is absent or unavailable."""
    text = _fallback_prep(text)
    debt_item = _fallback_debt_parse(text)''')

rep('''    m = re.search(r"\\b(?:gave|lent|lend)\\s+(?!\\d)(.+?)\\s+" + N + r"\\b", tl)''',
    '''    m = re.search(r"\\b(?:split|share)\\s+" + N + r"\\b(.*?)\\bwith\\s+(.+)$", tl)  # "split 600 dinner with rahul"
    if m:
        amount, people = _amount_from_str(m.group(1)), _take_people(m.group(3))
        if amount and people:
            pre = m.group(2)
            return _validate_debt_item({
                "share_type": "split", "amount": amount, "people": people, "category": _guess_category(pre),
                "payment_method": _method_in(tl), "note": _leftover_note(pre),
            })

    m = re.search(r"\\b(?:gave|lent|lend)\\s+(?!\\d)(.+?)\\s+" + N + r"\\b", tl)''')

rep('''    ("food", ["lunch", "dinner", "breakfast", "food", "cafe", "restaurant", "burger", "pizza", "juice",
              "biscuit", "tea", "coffee", "snack"]),''', '''    ("food", ["lunch", "dinner", "breakfast", "food", "cafe", "restaurant", "burger", "pizza", "juice",
              "biscuit", "tea", "coffee", "snack", "chai", "samosa", "dosa", "biryani", "swiggy", "zomato"]),''')
rep('''    ("transport", ["uber", "cab", "bus", "train", "metro", "flight", "transit", "petrol", "fuel"]),''',
    '''    ("transport", ["uber", "cab", "taxi", "ola", "rapido", "auto", "rickshaw", "bus", "train", "metro", "flight",
                   "transit", "petrol", "diesel", "fuel", "parking", "toll"]),''')

# ============================================================ 6. LLM output validation
rep('''def _validate_item(data):''', '''def _coerce_amount(v) -> Decimal:
    """'1,200' / '₹1200' / 1200 -> Decimal. NaN/Infinity raise (a NaN used to blow up the WHOLE parse)."""
    if isinstance(v, str):
        v = v.replace(",", "").replace("₹", "").strip()
    d = _to_decimal(v)
    if not d.is_finite():
        raise InvalidOperation("non-finite amount")
    return d


def _lc(v):
    return v.strip().lower() if isinstance(v, str) else None


def _validate_item(data):''')
rep('        amount = _to_decimal(data["amount"])\n', '        amount = _coerce_amount(data["amount"])\n', count=2)
rep('''    category = data.get("category") if data.get("category") in valid_categories else "other"
    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else None''',
    '''    category = _lc(data.get("category")) if _lc(data.get("category")) in valid_categories else "other"
    payment_method = _lc(data.get("payment_method")) if _lc(data.get("payment_method")) in PAYMENT_METHODS else None''')
rep('''    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else "cash"
    category = data.get("category") if data.get("category") in EXPENSE_CATEGORIES else "other"''',
    '''    payment_method = _lc(data.get("payment_method")) if _lc(data.get("payment_method")) in PAYMENT_METHODS else "cash"
    category = _lc(data.get("category")) if _lc(data.get("category")) in EXPENSE_CATEGORIES else "other"''')
rep('''    if share_type in ("contribution", "pool_expense") and not pool:
        return None
''', '''    if share_type in ("contribution", "pool_expense") and not pool:
        return None
    if len(people) > 20:
        return None  # 20+ sequential writes would blow the serverless time limit
    if share_type == "split" and _to_decimal(amount / (len(people) + 1)) <= 0:
        return None  # my share would round to 0.00
''')
rep('''            results = []
            for x in raw_items:
                v = _validate_debt_item(x) if x.get("share_type") else _validate_item(x)
                if v is not None:
                    results.append(v)
            if results:
                return _demote_unbacked_debt(results, text)''', '''            results, dropped = [], 0
            for x in raw_items:
                v = None
                if isinstance(x, dict):
                    v = _validate_debt_item(x) if x.get("share_type") else _validate_item(x)
                if v is not None:
                    results.append(v)
                else:
                    dropped += 1
            if results:
                if dropped:  # used to vanish silently while the reply claimed success
                    _tl.notices.append(f"{dropped} item(s) in your message could not be read and were NOT logged.")
                return _demote_unbacked_debt(results, text)''')

# ============================================================ 7. aliases: never lock in "other"
rep('''    if not _alias_ok(note_key, tx_type) or not category:
        return
    existing = _get_alias_row(note_key)''', '''    if not _alias_ok(note_key, tx_type) or not category or category == "other":
        return  # "other" is a non-answer; confirming it would make the alias skip the LLM forever
    existing = _get_alias_row(note_key)''')

# ============================================================ 8. data layer: keyset paging, bulk inserts, RPC balances
rep('''def _all_transactions(order="id.desc", limit=None, since=None):
    """Paginates through PostgREST's max-rows cap, so limit=100000 really returns up to 100000 rows.
    SB_PAGE_SIZE must be <= your project's 'Max rows' setting (Supabase default is 1000)."""
    if _use_sb():
        out = []
        offset = 0
        while True:
            page = SB_PAGE_SIZE if limit is None else min(SB_PAGE_SIZE, limit - len(out))
            if page <= 0:
                break
            params = {"select": "*", "order": order, "limit": page, "offset": offset}
            if since:
                params["created_at"] = f"gte.{since}"
            chunk = _rows(_sb("GET", "transactions", params=params))
            out.extend(chunk)
            if len(chunk) < page:
                break
            offset += page
        return out

    rows = list(_mem_transactions)
    if since:
        rows = [r for r in rows if r["created_at"] >= since]''', '''def _all_transactions(order="id.desc", limit=None, since=None, tx_type=None):
    """Keyset-paginates (id < last_id) through PostgREST's max-rows cap. OFFSET paging could return the
    same row twice (or skip one) when a row was inserted between two pages, which skewed balances.
    SB_PAGE_SIZE must be <= your project's 'Max rows' setting (Supabase default is 1000)."""
    if _use_sb():
        out, last_id = [], None
        desc = order.endswith(".desc")
        while True:
            page = SB_PAGE_SIZE if limit is None else min(SB_PAGE_SIZE, limit - len(out))
            if page <= 0:
                break
            params = {"select": "*", "order": "id.desc" if desc else "id.asc", "limit": page}
            if since:
                params["created_at"] = f"gte.{since}"
            if tx_type:
                params["type"] = f"eq.{tx_type}"
            if last_id is not None:
                params["id"] = f"{'lt' if desc else 'gt'}.{last_id}"
            chunk = _rows(_sb("GET", "transactions", params=params))
            out.extend(chunk)
            if len(chunk) < page:
                break
            last_id = chunk[-1]["id"]
        return out

    rows = list(_mem_transactions)
    if tx_type:
        rows = [r for r in rows if r["type"] == tx_type]
    if since:
        rows = [r for r in rows if r["created_at"] >= since]''')

rep('''def _all_transactions(order''', '''def add_transactions_bulk(items) -> list:
    """Insert several plain transactions in ONE request (all-or-nothing on Supabase). Returns ids in order."""
    if len(items) == 1 or not _use_sb():
        return [add_transaction(i["type"], i["amount"], i["category"], i["note"], i["payment_method"]) for i in items]
    body = [{"type": i["type"], "amount": str(_to_decimal(i["amount"])), "category": i["category"],
             "note": i["note"], "payment_method": i["payment_method"]} for i in items]
    rows = _rows(_sb("POST", "transactions", json_body=body, prefer="return=representation"))
    if len(rows) != len(items) or any("id" not in r for r in rows):
        raise StorageError("Supabase bulk insert returned an unexpected number of rows")
    return [r["id"] for r in sorted(rows, key=lambda r: r["id"])]


def add_transfers_bulk(rows) -> None:
    """Insert several transfer rows in ONE request (all-or-nothing on Supabase)."""
    if not rows:
        return
    if _use_sb():
        body = [{"type": "transfer", "amount": str(_to_decimal(r["amount"])), "category": "transfer",
                 "from_bucket": r["from_bucket"], "to_bucket": r["to_bucket"], "note": r.get("note"),
                 "person": r.get("person"), "pool": r.get("pool"), "linked_tx_id": r.get("linked_tx_id")}
                for r in rows]
        _sb("POST", "transactions", json_body=body, prefer="return=minimal")
        return
    for r in rows:
        add_transfer(r["amount"], r["from_bucket"], r["to_bucket"], r.get("note"),
                     person=r.get("person"), pool=r.get("pool"), linked_tx_id=r.get("linked_tx_id"))


def _all_transactions(order''')

rep('''    rows = _all_transactions()
    result = get_starting_balances()
    for r in rows:
        rtype = r.get("type")
        if rtype == "transfer":
            frm = r.get("from_bucket") or "cash"
            to = r.get("to_bucket") or "cash"
            result.setdefault(frm, Decimal("0.00"))''', '''    result = get_starting_balances()
    totals = _rpc_bucket_balances()  # one round trip instead of downloading every row (migration 003)
    if totals is not None:
        for bucket, amt in totals.items():
            result[bucket] = result.get(bucket, Decimal("0.00")) + amt
        return result
    rows = _all_transactions()
    for r in rows:
        rtype = r.get("type")
        if rtype == "transfer":
            frm = r.get("from_bucket") or "cash"
            to = r.get("to_bucket") or "cash"
            result.setdefault(frm, Decimal("0.00"))''')

rep('''def get_balances_by_method() -> dict:''', '''_RPC = {"ok": None}  # None = not tried yet, False = function not installed (use the slow path)


def _rpc_bucket_balances():
    if not _use_sb() or _RPC["ok"] is False:
        return None
    try:
        rows = _rows(_sb("POST", "rpc/bucket_balances", json_body={}))
    except StorageError as e:
        if e.status in (400, 404):
            _RPC["ok"] = False
            print("[balances] rpc/bucket_balances not installed - run 003_safety_and_speed.sql for faster balances")
            return None
        raise
    _RPC["ok"] = True
    return {r["bucket"]: Decimal(str(r["total"])) for r in rows}


def get_balances_by_method() -> dict:''')

rep('''    rows = _all_transactions(since=since)
    return sum((Decimal(str(r["amount"])) for r in rows if r["type"] == "expense"), Decimal("0.00"))''',
    '''    rows = _all_transactions(since=since, tx_type="expense")
    return sum((Decimal(str(r["amount"])) for r in rows), Decimal("0.00"))''')
rep('            c = r["category"]\n', '            c = r.get("category") or "other"\n')

# ---- split: bulk + no zero rows + compensating rollback
rep('''        per_person = _to_decimal(total_others / len(people))
        running = Decimal("0.00")
        breakdown = []
        for i, p in enumerate(people):
            amt_i = per_person if i < len(people) - 1 else (total_others - running)
            running += amt_i
            add_transfer(amt_i, method, f"iou:{p}", note, person=p, linked_tx_id=tx_id)
            breakdown.append((p, amt_i))
''', '''        per_person = _to_decimal(total_others / len(people))
        running = Decimal("0.00")
        breakdown, rows = [], []
        for i, p in enumerate(people):
            amt_i = per_person if i < len(people) - 1 else (total_others - running)
            running += amt_i
            if amt_i > 0:  # never write 0.00 transfer rows
                rows.append({"amount": amt_i, "from_bucket": method, "to_bucket": f"iou:{p}",
                             "note": note, "person": p, "linked_tx_id": tx_id})
            breakdown.append((p, amt_i))
        try:
            add_transfers_bulk(rows)  # ONE request instead of N
        except StorageError:
            try:
                delete_transaction(tx_id)  # don't leave my-share expense behind without its IOUs
            except StorageError:
                pass
            raise
''')
rep('''        tx_id = add_transfer(amount, method, f"pool:{pool}", note, pool=pool)
        add_pool_contribution(pool, "me", amount, note, linked_tx_id=tx_id)''',
    '''        tx_id = add_transfer(amount, method, f"pool:{pool}", note, pool=pool)
        try:
            add_pool_contribution(pool, "me", amount, note, linked_tx_id=tx_id)
        except StorageError:
            try:
                delete_transaction(tx_id)
            except StorageError:
                pass
            raise''')

# ---- undo that knows about pool rows
rep('''def delete_pool_contribution(row_id) -> bool:''', '''def _ts(row):
    try:
        dt = datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _last_pool_row(table):
    """Newest pool row that is NOT the twin of a transfer (those are removed with their transfer)."""
    if _use_sb():
        params = {"select": "*", "order": "created_at.desc", "limit": 1}
        if table == "pool_contributions":
            params["linked_tx_id"] = "is.null"
        rows = _rows(_sb("GET", table, params=params))
        return rows[0] if rows else None
    src = _mem_pool_contributions if table == "pool_contributions" else _mem_pool_expenses
    cand = [r for r in src if not (table == "pool_contributions" and r.get("linked_tx_id"))]
    return max(cand, key=_ts) if cand else None


def undo_last_action():
    """Undo the newest thing the user did across transactions AND pool rows. /undo used to look at the
    transactions table only, so after '300 from goa pool' it silently reverted an OLDER entry instead.
    Returns (reply_text, rows_removed) or None when there is nothing to undo."""
    tx = (_all_transactions(limit=1) or [None])[0]
    cands = [c for c in (("tx", tx), ("pc", _last_pool_row("pool_contributions")),
                         ("pe", _last_pool_row("pool_expenses"))) if c[1]]
    if not cands:
        return None
    kind, row = max(cands, key=lambda c: _ts(c[1]))  # ties go to the transaction (first in list)
    if kind == "pc":
        if not delete_pool_contribution(row["id"]):
            return None
        return f"Reverted {_pn(row['person'])}'s \u20b9{fmt(row['amount'])} deposit into {str(row['pool']).upper()} pool.", 1
    if kind == "pe":
        if not delete_pool_expense(row["id"]):
            return None
        return f"Reverted \u20b9{fmt(row['amount'])} spent from {str(row['pool']).upper()} pool.", 1
    group = delete_transaction_group(row)
    if not group:
        return None
    if len(group) == 1:
        r = group[0]
        if r["type"] == "transfer":
            return f"Reverted #{r['id']}: transfer \u20b9{fmt(r['amount'])} ({r.get('from_bucket')} \u2192 {r.get('to_bucket')}).", 1
        return f"Reverted #{r['id']}: {r['type']} \u20b9{fmt(r['amount'])} ({r.get('category')}).", 1
    anchor = next((r for r in group if r["type"] != "transfer"), group[0])
    return f"Reverted last action: {len(group)} linked entries (\u20b9{fmt(anchor['amount'])} {anchor['type']}).", len(group)


def delete_pool_contribution(row_id) -> bool:''')

rep_re(r'''        elif cmd == "/undo":\n            group = delete_last_transaction\(\).*?(?=        elif cmd == "/delete":)''',
       '''        elif cmd == "/undo":
            undone = undo_last_action()
            if not undone:
                reply_text = "Nothing to undo."
                log_chat_message("assistant", reply_text)
                return {"ok": True, "text": reply_text, **_totals()}
            reply_text, n = undone
            log_chat_message("assistant", reply_text)
            return {
                "ok": True,
                "text": reply_text,
                "html": f"<p>Reverted <strong>{n}</strong> linked entr{'y' if n == 1 else 'ies'} from the treasury purse.</p>",
                **_totals(),
            }

''')

rep('''        group = delete_last_transaction()
        if not group:
            return _totals({"ok": True, "text": "Nothing to undo."})
        n = len(group)
        return _totals({"ok": True, "text": f"Reverted last action ({n} linked entr{'y' if n == 1 else 'ies'})."})''',
    '''        undone = undo_last_action()
        if not undone:
            return _totals({"ok": True, "text": "Nothing to undo."})
        return _totals({"ok": True, "text": undone[0]})''')

# ---- the "balance + today_spent" pair repeated 21x
rep('''        out = {"balance": float(get_balance()), "today_spent": float(get_today_expense())}
        out.update(extra)''', '''        out = _balance_pair()
        out.update(extra)''')
rep_re(r'"balance": float\(get_balance\(\)\),\s*"today_spent": float\(get_today_expense\(\)\)', '**_balance_pair()', count=18)
rep('''def _unwrap(res):''', '''def _balance_pair() -> dict:
    return {"balance": float(get_balance()), "today_spent": float(get_today_expense())}


def _unwrap(res):''')
# ---- /undo above calls _totals() with no args; give module level one
rep('''def _balance_pair() -> dict:''', '''def _totals(extra=None) -> dict:
    out = _balance_pair()
    out.update(extra or {})
    return out


def _balance_pair() -> dict:''')

# ---- multi-item write path: bulk insert + skip pointless alias reads
rep('''    logged = []
    for parsed in transactions:
        if parsed.get("share_type"):''', '''    logged = []
    plain_items = [p for p in transactions if not p.get("share_type")]
    plain_ids = {}
    if len(plain_items) > 1:
        plain_ids = {id(p): i for p, i in zip(plain_items, add_transactions_bulk(plain_items))}
    for parsed in transactions:
        if parsed.get("share_type"):''')
rep('''            tx_id = add_transaction(parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
            note_key = (parsed.get("note") or "").lower().strip()
            _soft_learn_auto(note_key, parsed["type"], parsed["category"], parsed["payment_method"])''',
    '''            tx_id = plain_ids.get(id(parsed)) or add_transaction(
                parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
            note_key = (parsed.get("note") or "").lower().strip()
            if note_key and note_key not in aliases:  # already-confirmed aliases need no extra DB round trips
                _soft_learn_auto(note_key, parsed["type"], parsed["category"], parsed["payment_method"])''')

# ============================================================ 9. output hygiene
rep('''        c_name = cat_item["category"].capitalize()''', '''        c_name = _esc(str(cat_item["category"] or "other").capitalize())''')
rep('''<span>#{r['id']} transfer {frm} \u2192 {to}</span>''', '''<span>#{r['id']} transfer {_esc(str(frm))} \u2192 {_esc(str(to))}</span>''')
rep('''<span>#{r['id']} {r['category']}{note}{pay}</span>''', '''<span>#{r['id']} {_esc(str(r['category'] or 'other'))}{note}{pay}</span>''')

rep('''    writer = csv.writer(buf)
    writer.writerow(["id",''', '''    writer = csv.writer(buf)

    def _csv_safe(v):
        # a note like "=HYPERLINK(...)" would run as a formula when the CSV is opened in Excel/Sheets
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\\t", "\\r") else v

    writer.writerow(["id",''')
rep('''        writer.writerow([
            r.get("id"), r.get("type"), r.get("amount"), r.get("category"), r.get("note"),
            r.get("payment_method"), r.get("from_bucket"), r.get("to_bucket"), r.get("person"),
            r.get("pool"), r.get("linked_tx_id"), r.get("created_at"),
        ])''', '''        writer.writerow([_csv_safe(c) for c in (
            r.get("id"), r.get("type"), r.get("amount"), r.get("category"), r.get("note"),
            r.get("payment_method"), r.get("from_bucket"), r.get("to_bucket"), r.get("person"),
            r.get("pool"), r.get("linked_tx_id"), r.get("created_at"),
        )])''')
rep("output.write(buf.getvalue().encode('utf-8'))", "output.write(buf.getvalue().encode('utf-8-sig'))  # BOM: Excel shows unicode correctly")

# ------------------------------------------------------------------ write
out = src.replace("\n", "\r\n") if crlf else src
try:
    compile(src, PATH, "exec")
except SyntaxError as e:
    sys.exit(f"PATCH FAILED: result does not compile ({e}). Nothing was changed.")
open(PATH, "wb").write(out.encode("utf-8"))
print("api/index.py patched OK.")
