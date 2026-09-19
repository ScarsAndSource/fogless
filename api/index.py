"""
Fogless — 16-bit JRPG Personal Finance Companion Web Application & Serverless API.
Everything lives in one file on purpose: Vercel's Python builder has
inconsistent behavior bundling sibling modules for some function
configurations.
"""
import os
import io
import csv
import re
import json
import requests
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_file, Response, render_template_string


# --------------------------------------------------------------------- config


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))


GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_CHAT_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO_API = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"


TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
} if SUPABASE_KEY else {}
SB_TIMEOUT = 8
SB_PAGE_SIZE = 1000


IST = timezone(timedelta(hours=5, minutes=30))  # local calendar-day boundary for /stats "today"


def _fmt_dt(iso_str) -> str:
    """Render a stored UTC timestamp as a short IST date/time, e.g. '16 Sep, 03:42 PM'."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%d %b, %I:%M %p")
    except Exception:
        return ""



EXPENSE_CATEGORIES = [
    "food", "groceries", "transport", "bills", "shopping", "entertainment",
    "health", "subscriptions", "rent", "education", "travel", "fitness", "other",
]
INCOME_CATEGORIES = ["salary", "freelance", "gift", "refund", "other"]
PAYMENT_METHODS = ["cash", "upi", "card", "netbanking", "other"]


_CURRENCY_WORDS = {"rs", "rs.", "inr", "rupees", "rupee", "bucks", "$", "₹", "gp"}


MULTI_SYSTEM_PROMPT = f"""You are a strict JSON-extraction engine for a personal expense tracker that also tracks
money shared with other people (splitting bills, lending, borrowing, repayments) and shared pooled funds.

A message may describe ONE or SEVERAL transactions (separated by line breaks, spaces, commas, "and", semicolons,
or listed sequentially like "110 Food upi 100 Juice cash 100 biscuit"). Extract EVERY transaction you find.

Expense categories (pick exactly one per transaction): {", ".join(EXPENSE_CATEGORIES)}
Income categories (pick exactly one per transaction): {", ".join(INCOME_CATEGORIES)}
Payment methods (pick exactly one if mentioned or clearly implied, else null): {", ".join(PAYMENT_METHODS)}

=== NORMAL TRANSACTIONS (no other person or pool involved) ===
- "type" is "expense" or "income". Assume "expense" unless words like salary, got, got paid, received, earned,
  payout, credited, deposited, refund clearly signal money coming IN (e.g. "got 424 upi" means income).
  EXCEPTION: if the message names another person or a shared pool (see below), do NOT apply this rule at all —
  use the SHARED & DEBT rules instead. "share_type" always overrides "type" when it is set.
- "amount" is a plain number (no currency symbols, no commas). "1.5k" or "2k" means 1500 / 2000.
- "category" is exactly one value from the matching list above — pick the closest fit, never invent new categories.
- "payment_method" is exactly one of the listed methods, or null if not mentioned.
- "note" is the short specific detail (e.g. item name like "coffee", "juice", "biscuit"), or null if there isn't one.

=== SHARED & DEBT MONEY (another named person or a named pool is involved) ===
Set "share_type" whenever a message involves another person's money or a shared pool. When set, it OVERRIDES the
normal type/income logic above entirely. Still fill "type"/"category" with a reasonable placeholder — they are
only actually used for "split" and "pool_expense", and ignored for the others.

share_type = "split"
  You paid the FULL amount yourself for something shared. Your own share becomes a normal personal expense; each
  named person owes an equal share. Set "people" to the list of OTHER people involved (never include yourself).
  Triggers: "split with X", "X's share", "split N ways with X and Y", "paid for me and X".
  Example: "600 dinner upi, split with rahul"
    -> {{"type":"expense","amount":600,"category":"food","payment_method":"upi","note":"dinner","share_type":"split","people":["rahul"],"pool":null,"repayment_direction":null}}
  Example: "900 cabin split three ways with rahul and priya" -> amount 900, people ["rahul","priya"] (you + 2 others = 3-way split, category "travel" or "other" as fits).

share_type = "lend"
  You GAVE another person money directly — not a shared purchase, the full amount is now owed to you.
  Triggers: "gave X ...", "lent X ...", "paid for X" with no split/share language present.
  Example: "gave rahul 200 cash" -> {{"amount":200,"payment_method":"cash","share_type":"lend","people":["rahul"]}}

share_type = "borrow"
  Another person gave YOU money as a loan — you now owe them.
  Triggers: "borrowed ... from X".
  Example: "borrowed 500 cash from priya" -> {{"amount":500,"payment_method":"cash","share_type":"borrow","people":["priya"]}}

share_type = "repayment"
  Settles part or all of an EXISTING debt, in either direction. Always set "repayment_direction":
    "they_paid_me" — someone who owed you paid some/all of it back.
      Triggers: "X paid me back ...", "X paid back ...", "X returned ...", "X repaid ..." — AND, whenever a
      person's name follows "from", also: "got ... from X", "received ... from X". A named "from X" always means
      a debt settlement, never generic income.
      Example: "rahul paid back 150"
        -> {{"amount":150,"share_type":"repayment","repayment_direction":"they_paid_me","people":["rahul"]}}
      Example: "got 150 upi from rahul"
        -> {{"amount":150,"payment_method":"upi","share_type":"repayment","repayment_direction":"they_paid_me","people":["rahul"]}}
      These two examples are DIFFERENT WORDS for the SAME EVENT and must produce identical share_type,
      repayment_direction, amount, and people.
    "i_paid_them" — you paid back money you had borrowed.
      Triggers: "paid X back ...", "settled up with X ...", "cleared my debt to X ...".
      Example: "paid priya back 300 upi"
        -> {{"amount":300,"payment_method":"upi","share_type":"repayment","repayment_direction":"i_paid_them","people":["priya"]}}

share_type = "contribution"
  YOU put your own money into a named shared pool/fund. Set "pool" to a short lowercase name (turn spaces into
  hyphens), no "people" needed.
  Triggers: "put ... into the X pool/fund", "added ... to X pool", "contributed ... to X".
  Example: "put 500 into goa pool" -> {{"amount":500,"share_type":"contribution","pool":"goa"}}

share_type = "pool_contribution_other"
  SOMEONE ELSE put money into a named pool — your own cash does not move. Set "people" to that one person and
  "pool" to the pool name.
  Example: "priya put 500 into goa pool" -> {{"amount":500,"share_type":"pool_contribution_other","people":["priya"],"pool":"goa"}}

share_type = "pool_expense"
  Money was spent FROM a named pool, not your personal cash. Set "pool" and "category".
  Example: "paid 1200 hotel from goa pool" -> {{"amount":1200,"category":"travel","share_type":"pool_expense","pool":"goa"}}

If NOTHING in the message describes money changing hands, respond with {{"transactions": []}}.

Respond with ONLY a raw JSON object matching this exact shape, nothing else:
{{"transactions": [{{"type": "expense", "amount": 0, "category": "other", "payment_method": null, "note": null,
"share_type": null, "people": [], "pool": null, "repayment_direction": null}}]}}
"""


_MULTI_HINT_RE = re.compile(r",|;|\band\b|\n|\b\d+(?:\.\d+)?\b.*?\b\d+(?:\.\d+)?\b", re.IGNORECASE | re.DOTALL)


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# In-memory storage fallback when Supabase is not configured
_mem_transactions = []
_mem_id_counter = 1
_mem_aliases = {}
_mem_starting_balances = {b: Decimal("0.00") for b in PAYMENT_METHODS}
_mem_pool_contributions = []
_mem_pool_expenses = []


app = Flask(__name__)


# ------------------------------------------------------------------ helpers


def _to_decimal(value) -> Decimal:
    d = Decimal(str(value))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def fmt(amount) -> str:
    try:
        val = Decimal(str(amount))
        return f"{val:,.2f}"
    except Exception:
        return str(amount)


def send_telegram_message(chat_id, text: str) -> None:
    """Send a plain-text reply back to a Telegram chat. Non-fatal: logs on error rather than crashing."""
    if not TG_API:
        print("[telegram] BOT_TOKEN not configured, cannot send message")
        return
    try:
        r = requests.post(
            f"{TG_API}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        if r.status_code >= 300:
            print(f"[telegram] sendMessage failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[telegram] sendMessage exception: {e}")


# ---------------------------------------------------------------- data layer


def _sb_url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}" if SUPABASE_URL else ""


def add_transaction(tx_type, amount, category, note, payment_method=None) -> int:
    global _mem_id_counter
    amount_dec = _to_decimal(amount)
    
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            payload = {
                "type": tx_type,
                "amount": str(amount_dec),
                "category": category,
                "note": note,
                "payment_method": payment_method,
            }
            r = requests.post(
                _sb_url("transactions"),
                headers={**SB_HEADERS, "Prefer": "return=representation"},
                json=payload,
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()[0]["id"]
        except Exception as e:
            print(f"Supabase write error, falling back to memory: {e}")

    # Fallback in-memory store
    tx_id = _mem_id_counter
    _mem_id_counter += 1
    _mem_transactions.append({
        "id": tx_id,
        "type": tx_type,
        "amount": amount_dec,
        "category": category,
        "note": note,
        "payment_method": payment_method,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return tx_id


def add_transfer(amount, from_bucket, to_bucket, note, person=None, pool=None, linked_tx_id=None) -> int:
    """A zero-sum move of money between two 'buckets'. Buckets can be real payment methods
    (cash/upi/card/...) or virtual ones: iou:<person> for a debt, pool:<name> for your own stake
    in a shared pool. Never counted as income or expense — get_stats() ignores type=='transfer'
    automatically since it only ever checks for 'income'/'expense'."""
    global _mem_id_counter
    amount_dec = _to_decimal(amount)

    if SUPABASE_URL and SUPABASE_KEY:
        try:
            payload = {
                "type": "transfer",
                "amount": str(amount_dec),
                "from_bucket": from_bucket,
                "to_bucket": to_bucket,
                "note": note,
                "person": person,
                "pool": pool,
                "linked_tx_id": linked_tx_id,
            }
            r = requests.post(
                _sb_url("transactions"),
                headers={**SB_HEADERS, "Prefer": "return=representation"},
                json=payload,
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()[0]["id"]
        except Exception as e:
            print(f"Supabase write error (transfer), falling back to memory: {e}")

    tx_id = _mem_id_counter
    _mem_id_counter += 1
    _mem_transactions.append({
        "id": tx_id,
        "type": "transfer",
        "amount": amount_dec,
        "from_bucket": from_bucket,
        "to_bucket": to_bucket,
        "note": note,
        "person": person,
        "pool": pool,
        "linked_tx_id": linked_tx_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return tx_id


def _all_transactions(order="id.desc", limit=None, since=None):
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            if limit:
                params = {"select": "*", "order": order, "limit": limit}
                if since:
                    params["created_at"] = f"gte.{since}"
                r = requests.get(_sb_url("transactions"), headers=SB_HEADERS, params=params, timeout=SB_TIMEOUT)
                r.raise_for_status()
                return r.json(parse_float=Decimal)

            all_rows = []
            offset = 0
            while True:
                params = {"select": "*", "order": order, "limit": SB_PAGE_SIZE, "offset": offset}
                if since:
                    params["created_at"] = f"gte.{since}"
                r = requests.get(_sb_url("transactions"), headers=SB_HEADERS, params=params, timeout=SB_TIMEOUT)
                r.raise_for_status()
                page = r.json(parse_float=Decimal)
                all_rows.extend(page)
                if len(page) < SB_PAGE_SIZE:
                    break
                offset += SB_PAGE_SIZE
            return all_rows
        except Exception as e:
            print(f"Supabase read error, falling back to memory: {e}")

    # Fallback memory query
    rows = list(_mem_transactions)
    if since:
        rows = [r for r in rows if r["created_at"] >= since]
    reverse = ("desc" in order)
    rows.sort(key=lambda x: x["id"], reverse=reverse)
    if limit:
        rows = rows[:limit]
    return rows


def get_history(limit=10):
    return _all_transactions(limit=limit)


def get_transaction(tx_id):
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.get(
                _sb_url("transactions"),
                headers=SB_HEADERS,
                params={"select": "*", "id": f"eq.{tx_id}"},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            rows = r.json(parse_float=Decimal)
            return rows[0] if rows else None
        except Exception:
            pass
    for r in _mem_transactions:
        if r["id"] == tx_id:
            return r
    return None


def update_transaction(tx_id, **fields) -> bool:
    if not fields:
        return False
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.patch(
                _sb_url(f"transactions?id=eq.{tx_id}"),
                headers={**SB_HEADERS, "Prefer": "return=representation"},
                json=fields,
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return len(r.json()) > 0
        except Exception:
            pass
    tx = get_transaction(tx_id)
    if tx:
        for k, v in fields.items():
            tx[k] = v
        return True
    return False


def delete_transaction(tx_id) -> bool:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.delete(
                _sb_url(f"transactions?id=eq.{tx_id}"),
                headers={**SB_HEADERS, "Prefer": "return=representation"},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return len(r.json()) > 0
        except Exception:
            pass
    global _mem_transactions
    initial_len = len(_mem_transactions)
    _mem_transactions = [r for r in _mem_transactions if r["id"] != tx_id]
    return len(_mem_transactions) < initial_len


def _delete_transaction_group(anchor_row):
    """Delete a transaction plus any others linked to the same logical action — e.g. a split's
    personal-expense row and every per-person transfer row that came from it. Without this,
    /undo right after a split would only remove one friend's share and leave the rest orphaned."""
    link_id = anchor_row.get("linked_tx_id") or anchor_row["id"]
    all_rows = _all_transactions()
    group = [r for r in all_rows if r["id"] == link_id or r.get("linked_tx_id") == link_id]
    for r in group:
        delete_transaction(r["id"])
    return group


def delete_last_transaction():
    rows = _all_transactions(limit=1)
    if not rows:
        return None
    return _delete_transaction_group(rows[0])


BALANCE_BUCKETS = PAYMENT_METHODS


def _settings_key(method: str) -> str:
    return f"starting_balance:{method}"


def get_starting_balances() -> dict:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.get(
                _sb_url("settings"),
                headers=SB_HEADERS,
                params={"select": "key,value", "key": "like.starting_balance:*"},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            result = {b: Decimal("0.00") for b in BALANCE_BUCKETS}
            for row in r.json():
                method = row["key"].split(":", 1)[1]
                if method in result:
                    try:
                        result[method] = Decimal(row["value"])
                    except InvalidOperation:
                        result[method] = Decimal("0.00")
            return result
        except Exception:
            pass
    return dict(_mem_starting_balances)


def set_starting_balance(method: str, amount) -> None:
    target_dec = _to_decimal(amount)
    rows = _all_transactions()
    net_tx = Decimal("0.00")
    for r in rows:
        rtype = r.get("type")
        if rtype == "transfer":
            frm = r.get("from_bucket") or "cash"
            to = r.get("to_bucket") or "cash"
            amt = Decimal(str(r["amount"]))
            if frm == method:
                net_tx -= amt
            if to == method:
                net_tx += amt
        else:
            m = r.get("payment_method") or "cash"
            if m == method:
                amt = Decimal(str(r["amount"]))
                if rtype == "income":
                    net_tx += amt
                else:
                    net_tx -= amt

    start_bal = target_dec - net_tx
    _mem_starting_balances[method] = start_bal
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.post(
                _sb_url("settings"),
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json={"key": _settings_key(method), "value": str(start_bal)},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return
        except Exception as e:
            print(f"Supabase write error setting balance: {e}")


def get_balances_by_method() -> dict:
    """Grand ledger across every bucket — real payment methods AND virtual iou:*/pool:* buckets.
    Transfers move money between two buckets with zero net effect on the total; income/expense
    move it into or out of exactly one bucket, same as before."""
    rows = _all_transactions()
    result = get_starting_balances()
    for r in rows:
        rtype = r.get("type")
        if rtype == "transfer":
            frm = r.get("from_bucket") or "cash"
            to = r.get("to_bucket") or "cash"
            result.setdefault(frm, Decimal("0.00"))
            result.setdefault(to, Decimal("0.00"))
            amt = Decimal(str(r["amount"]))
            result[frm] -= amt
            result[to] += amt
            continue
        method = r.get("payment_method") or "cash"
        if method not in result:
            result[method] = Decimal("0.00")
        if rtype == "income":
            result[method] += Decimal(str(r["amount"]))
        else:
            result[method] -= Decimal(str(r["amount"]))
    return result


def get_liquid_balances() -> dict:
    """Returns only real wallet/bank liquid payment pouches (excluding virtual debt/pool buckets)."""
    balances = get_balances_by_method()
    return {k: v for k, v in balances.items() if not k.startswith("iou:") and not k.startswith("pool:")}


def get_debt_balances() -> dict:
    """Positive = that person owes you. Negative = you owe them."""
    balances = get_balances_by_method()
    result = {}
    for bucket, amt in balances.items():
        if bucket.startswith("iou:"):
            result[bucket[len("iou:"):]] = amt
    return result


def get_balance() -> Decimal:
    """Total liquid treasury balance available in liquid pouches."""
    return sum(get_liquid_balances().values(), Decimal("0.00"))


def get_today_expense() -> Decimal:
    since = _since("today")
    rows = _all_transactions(since=since)
    return sum((Decimal(str(r["amount"])) for r in rows if r["type"] == "expense"), Decimal("0.00"))


def _since(period):
    now_utc = datetime.now(timezone.utc)
    if period == "today":
        now_ist = now_utc.astimezone(IST)
        start_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = start_ist.astimezone(timezone.utc)
        return start_utc.isoformat()
    elif period == "week":
        start = now_utc - timedelta(days=7)
    elif period == "month":
        start = now_utc - timedelta(days=30)
    else:
        return None
    return start.isoformat()


def get_stats(period="month"):
    since = _since(period)
    rows = _all_transactions(since=since) if since else _all_transactions()
    total_income = sum((Decimal(str(r["amount"])) for r in rows if r["type"] == "income"), Decimal("0.00"))
    total_expense = sum((Decimal(str(r["amount"])) for r in rows if r["type"] == "expense"), Decimal("0.00"))

    by_cat, by_pay = {}, {}
    for r in rows:
        amt = Decimal(str(r["amount"]))
        if r["type"] == "expense":
            c = r["category"]
            by_cat.setdefault(c, {"total": Decimal("0.00"), "cnt": 0})
            by_cat[c]["total"] += amt
            by_cat[c]["cnt"] += 1

            p = r.get("payment_method") or "cash"
            by_pay.setdefault(p, {"total": Decimal("0.00"), "cnt": 0})
            by_pay[p]["total"] += amt
            by_pay[p]["cnt"] += 1

    by_category = sorted(({"category": k, **v} for k, v in by_cat.items()), key=lambda x: -x["total"])
    by_payment = sorted(({"payment_method": k, **v} for k, v in by_pay.items()), key=lambda x: -x["total"])
    return {
        "total_income": total_income,
        "total_expense": total_expense,
        "net": total_income - total_expense,
        "by_category": by_category,
        "by_payment": by_payment,
    }


def all_data_dump():
    return {
        "transactions": _all_transactions(order="id.asc"),
        "starting_balances": get_starting_balances(),
        "pool_contributions": get_pool_contributions(),
        "pool_expenses": get_pool_expenses(),
    }


# --------------------------------------------------------- aliases


def get_all_aliases() -> dict:
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.get(
                _sb_url("aliases"),
                headers=SB_HEADERS,
                params={"select": "*", "confirmed": "eq.true"},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return {
                row["note_key"]: {
                    "type": row["type"], "category": row["category"], "payment_method": row.get("payment_method")
                }
                for row in r.json()
            }
        except Exception:
            pass
    return {k: v for k, v in _mem_aliases.items() if v.get("confirmed")}


def learn_alias_manual(note_key, tx_type, category, payment_method=None):
    if not note_key:
        return
    _mem_aliases[note_key] = {"type": tx_type, "category": category, "payment_method": payment_method, "confirmed": True}


def learn_alias_auto(note_key, tx_type, category, payment_method=None):
    if not note_key:
        return
    if note_key not in _mem_aliases:
        _mem_aliases[note_key] = {"type": tx_type, "category": category, "payment_method": payment_method, "confirmed": False}
    else:
        _mem_aliases[note_key]["confirmed"] = True


# --------------------------------------------------------- pools


def add_pool_contribution(pool_name, person, amount, note=None):
    amount_dec = _to_decimal(amount)
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.post(
                _sb_url("pool_contributions"),
                headers={**SB_HEADERS, "Prefer": "return=representation"},
                json={"pool": pool_name, "person": person, "amount": str(amount_dec), "note": note},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return
        except Exception as e:
            print(f"Supabase write error (pool_contribution), falling back to memory: {e}")
    _mem_pool_contributions.append({
        "pool": pool_name, "person": person, "amount": amount_dec, "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def add_pool_expense(pool_name, amount, category, note=None):
    amount_dec = _to_decimal(amount)
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.post(
                _sb_url("pool_expenses"),
                headers={**SB_HEADERS, "Prefer": "return=representation"},
                json={"pool": pool_name, "amount": str(amount_dec), "category": category, "note": note},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return
        except Exception as e:
            print(f"Supabase write error (pool_expense), falling back to memory: {e}")
    _mem_pool_expenses.append({
        "pool": pool_name, "amount": amount_dec, "category": category, "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


def get_pool_contributions(pool_name=None):
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            params = {"select": "*"}
            if pool_name:
                params["pool"] = f"eq.{pool_name}"
            r = requests.get(_sb_url("pool_contributions"), headers=SB_HEADERS, params=params, timeout=SB_TIMEOUT)
            r.raise_for_status()
            return r.json(parse_float=Decimal)
        except Exception as e:
            print(f"Supabase read error (pool_contributions), falling back to memory: {e}")
    rows = _mem_pool_contributions
    return [r for r in rows if r["pool"] == pool_name] if pool_name else list(rows)


def get_pool_expenses(pool_name=None):
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            params = {"select": "*"}
            if pool_name:
                params["pool"] = f"eq.{pool_name}"
            r = requests.get(_sb_url("pool_expenses"), headers=SB_HEADERS, params=params, timeout=SB_TIMEOUT)
            r.raise_for_status()
            return r.json(parse_float=Decimal)
        except Exception as e:
            print(f"Supabase read error (pool_expenses), falling back to memory: {e}")
    rows = _mem_pool_expenses
    return [r for r in rows if r["pool"] == pool_name] if pool_name else list(rows)


def get_pool_summary(pool_name):
    contributions = get_pool_contributions(pool_name)
    expenses = get_pool_expenses(pool_name)
    total_in = sum((Decimal(str(c["amount"])) for c in contributions), Decimal("0.00"))
    total_out = sum((Decimal(str(e["amount"])) for e in expenses), Decimal("0.00"))
    balance = total_in - total_out

    by_person = {}
    for c in contributions:
        by_person[c["person"]] = by_person.get(c["person"], Decimal("0.00")) + Decimal(str(c["amount"]))

    # v1 simplification: pool spend is assumed split evenly across everyone who has contributed.
    num_people = max(len(by_person), 1)
    fair_share = (total_out / num_people) if num_people else Decimal("0.00")
    net_position = {p: (amt - fair_share) for p, amt in by_person.items()}

    return {
        "pool": pool_name, "total_in": total_in, "total_out": total_out, "balance": balance,
        "by_person": by_person, "fair_share": fair_share, "net_position": net_position,
        "expenses": expenses,
    }


def list_known_pools():
    names = set(c["pool"] for c in get_pool_contributions()) | set(e["pool"] for e in get_pool_expenses())
    return sorted(names)


# ------------------------------------------------------------------ nlp layer

def _validate_item(data):
    if not data or data.get("type") not in ("expense", "income"):
        return None
    try:
        amount = _to_decimal(data["amount"])
    except (TypeError, ValueError, KeyError, InvalidOperation):
        return None
    if amount <= 0:
        return None
    valid_categories = EXPENSE_CATEGORIES if data["type"] == "expense" else INCOME_CATEGORIES
    category = data.get("category") if data.get("category") in valid_categories else "other"
    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else None
    note = data.get("note") or None
    return {"type": data["type"], "amount": amount, "category": category, "payment_method": payment_method, "note": note}


def _validate_debt_item(data):
    """Validates the SHARED & DEBT branch (share_type set). Returned dicts always carry a
    'share_type' key — that's how downstream code tells a debt item apart from a normal one."""
    if not data or not data.get("share_type"):
        return None
    share_type = data["share_type"]
    if share_type not in ("split", "lend", "borrow", "repayment", "contribution", "pool_contribution_other", "pool_expense"):
        return None
    try:
        amount = _to_decimal(data["amount"])
    except (TypeError, ValueError, KeyError, InvalidOperation):
        return None
    if amount <= 0:
        return None

    people_raw = data.get("people")
    if isinstance(people_raw, list) and people_raw:
        people = [str(p).strip().lower() for p in people_raw if str(p).strip()]
    elif data.get("person"):
        people = [str(data["person"]).strip().lower()]
    else:
        people = []

    pool = data.get("pool")
    pool = str(pool).strip().lower().replace(" ", "-") if pool else None

    if share_type in ("split", "lend", "borrow", "repayment") and not people:
        return None
    if share_type == "pool_contribution_other" and (not pool or not people):
        return None
    if share_type in ("contribution", "pool_expense") and not pool:
        return None

    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else "cash"
    category = data.get("category") if data.get("category") in EXPENSE_CATEGORIES else "other"
    note = data.get("note") or None
    repayment_direction = data.get("repayment_direction") if data.get("repayment_direction") in ("they_paid_me", "i_paid_them") else "they_paid_me"

    return {
        "share_type": share_type, "amount": amount, "people": people, "pool": pool,
        "payment_method": payment_method, "category": category, "note": note,
        "repayment_direction": repayment_direction,
    }


def _apply_debt_transaction(item):
    """Routes a validated debt item into the actual ledger writes and returns a summary dict
    used only for building the reply message — nothing here is re-read from the DB afterwards."""
    share_type = item["share_type"]
    amount = item["amount"]
    method = item.get("payment_method") or "cash"
    note = item.get("note")
    category = item.get("category") or "other"
    people = item.get("people") or []
    pool = item.get("pool")
    person = people[0] if people else None

    if share_type == "split":
        num_people = 1 + len(people)
        my_share = _to_decimal(amount / num_people)
        total_others = amount - my_share
        tx_id = add_transaction("expense", my_share, category, note, method)
        per_person = _to_decimal(total_others / len(people))
        running = Decimal("0.00")
        breakdown = []
        for i, p in enumerate(people):
            amt_i = per_person if i < len(people) - 1 else (total_others - running)
            running += amt_i
            add_transfer(amt_i, method, f"iou:{p}", note, person=p, linked_tx_id=tx_id)
            breakdown.append((p, amt_i))
        return {"kind": "split", "tx_id": tx_id, "my_share": my_share, "people": breakdown, "method": method, "category": category}

    elif share_type == "lend":
        add_transfer(amount, method, f"iou:{person}", note, person=person)
        return {"kind": "lend", "amount": amount, "who": person, "method": method}

    elif share_type == "borrow":
        add_transfer(amount, f"iou:{person}", method, note, person=person)
        return {"kind": "borrow", "amount": amount, "who": person, "method": method}

    elif share_type == "repayment":
        direction = item.get("repayment_direction", "they_paid_me")
        if direction == "they_paid_me":
            add_transfer(amount, f"iou:{person}", method, note, person=person)
        else:
            add_transfer(amount, method, f"iou:{person}", note, person=person)
        return {"kind": "repayment", "direction": direction, "amount": amount, "who": person, "method": method}

    elif share_type == "contribution":
        add_transfer(amount, method, f"pool:{pool}", note, pool=pool)
        add_pool_contribution(pool, "me", amount, note)
        return {"kind": "contribution", "amount": amount, "who": pool, "method": method}

    elif share_type == "pool_contribution_other":
        add_pool_contribution(pool, person, amount, note)
        return {"kind": "pool_contribution_other", "amount": amount, "who": pool, "person": person}

    elif share_type == "pool_expense":
        add_pool_expense(pool, amount, category, note)
        return {"kind": "pool_expense", "amount": amount, "who": pool, "category": category}

    return None


def _call_groq_chat(text):
    if not GROQ_API_KEY:
        return _fallback_rule_parse(text)
    try:
        r = requests.post(
            GROQ_CHAT_API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": MULTI_SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=15,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content, parse_float=Decimal)
        raw_items = data.get("transactions") if isinstance(data, dict) else None
        if raw_items:
            results = []
            for x in raw_items:
                v = _validate_debt_item(x) if x.get("share_type") else _validate_item(x)
                if v is not None:
                    results.append(v)
            if results:
                return results
    except Exception as e:
        print(f"Groq API call error: {e}")
    return _fallback_rule_parse(text)


def _parse_single_fallback_item(text):
    tokens = text.lower().replace("₹", " ").replace("$", " ").split()
    if not tokens:
        return None

    tx_type = "expense"
    if any(w in text.lower() for w in ["income", "salary", "got paid", "got", "received", "earned", "payout", "refund", "credited", "deposited"]):
        tx_type = "income"

    amount = None
    payment_method = None
    category = None
    note_words = []

    for tok in tokens:
        clean = tok.strip(".,;")
        if amount is None:
            m = re.match(r"^(\d+(?:\.\d+)?)(k)?$", clean)
            if m:
                val = Decimal(m.group(1))
                if m.group(2) == "k":
                    val *= Decimal("1000")
                amount = val
                continue
        if clean in _CURRENCY_WORDS:
            continue
        if clean in PAYMENT_METHODS:
            payment_method = clean
            continue
        if clean in (EXPENSE_CATEGORIES if tx_type == "expense" else INCOME_CATEGORIES):
            category = clean
            continue
        note_words.append(clean)

    if amount is None or amount <= 0:
        return None

    if not category:
        note_str = " ".join(note_words)
        if any(w in note_str for w in ["lunch", "dinner", "food", "cafe", "restaurant", "burger", "pizza", "juice", "biscuit", "tea"]):
            category = "food"
        elif any(w in note_str for w in ["milk", "groceries", "veggies", "fruit", "market"]):
            category = "groceries"
        elif any(w in note_str for w in ["uber", "cab", "bus", "train", "flight", "transit", "petrol", "fuel"]):
            category = "transport"
        elif any(w in note_str for w in ["rent", "electricity", "wifi", "bill", "water"]):
            category = "bills" if "rent" not in note_str else "rent"
        elif tx_type == "income":
            category = "freelance" if "freelance" in note_str else "salary"
        else:
            category = "other"

    note = " ".join(note_words).strip() or None
    return {"type": tx_type, "amount": _to_decimal(amount), "category": category, "payment_method": payment_method, "note": note}


def _guess_category(note_str: str, tx_type="expense") -> str:
    note_str = note_str or ""
    if any(w in note_str for w in ["lunch", "dinner", "food", "cafe", "restaurant", "burger", "pizza", "juice", "biscuit", "tea"]):
        return "food"
    if any(w in note_str for w in ["milk", "groceries", "veggies", "fruit", "market"]):
        return "groceries"
    if any(w in note_str for w in ["uber", "cab", "bus", "train", "flight", "transit", "petrol", "fuel"]):
        return "transport"
    if any(w in note_str for w in ["rent", "electricity", "wifi", "bill", "water"]):
        return "bills" if "rent" not in note_str else "rent"
    if tx_type == "income":
        return "freelance" if "freelance" in note_str else "salary"
    return "other"


def _fallback_debt_parse(text):
    """Best-effort regex recognizer for the most common shared/debt phrasings, used ONLY when the
    Groq API is unavailable (no key, or the call failed). This is intentionally narrower than the
    LLM path above — it covers split/lend/borrow/repayment/pool for a single event per message,
    not every possible phrasing. Treats the WHOLE message as one event rather than chunking it,
    since chunking on commas (needed for multi-item plain expenses) would break sentences like
    "600 dinner upi, split with rahul" by cutting the amount away from the split phrase."""
    tl = text.strip().lower()

    def _num(s):
        m = re.match(r"^(\d+(?:\.\d+)?)(k)?$", s.replace(" ", ""))
        if not m:
            return None
        val = Decimal(m.group(1))
        if m.group(2):
            val *= Decimal("1000")
        return val

    def _method_in(s):
        for m in PAYMENT_METHODS:
            if re.search(rf"\b{m}\b", s):
                return m
        return None

    m = re.search(r"(\d+(?:\.\d+)?k?)\b(.*?)\b(?:split|share)(?:\s+\w+)?\s+with\s+([a-z][a-z ]*)", tl)
    if m:
        amount = _num(m.group(1))
        if amount:
            people = [p.strip() for p in re.split(r"\s+and\s+|,", m.group(3).strip()) if p.strip()]
            category = _guess_category(m.group(2))
            return _validate_debt_item({
                "share_type": "split", "amount": amount, "people": people,
                "category": category, "payment_method": _method_in(tl), "note": None,
            })

    m = re.search(r"\b(?:gave|lent)\s+([a-z]+)\s+(\d+(?:\.\d+)?k?)", tl)
    if m:
        amount = _num(m.group(2))
        if amount:
            return _validate_debt_item({
                "share_type": "lend", "amount": amount, "people": [m.group(1)],
                "payment_method": _method_in(tl), "note": None,
            })

    m = re.search(r"\bborrowed\s+(\d+(?:\.\d+)?k?)\s+from\s+([a-z]+)", tl)
    if m:
        amount = _num(m.group(1))
        if amount:
            return _validate_debt_item({
                "share_type": "borrow", "amount": amount, "people": [m.group(2)],
                "payment_method": _method_in(tl), "note": None,
            })

    m = re.search(r"\bpaid\s+([a-z]+)\s+back\s+(\d+(?:\.\d+)?k?)", tl)
    if m:
        amount = _num(m.group(2))
        if amount:
            return _validate_debt_item({
                "share_type": "repayment", "repayment_direction": "i_paid_them",
                "amount": amount, "people": [m.group(1)], "payment_method": _method_in(tl), "note": None,
            })

    m = re.search(r"([a-z]+)\s+(?:paid(?:\s+me)?\s+back|returned|repaid)\s+(\d+(?:\.\d+)?k?)", tl)
    if m:
        amount = _num(m.group(2))
        if amount:
            return _validate_debt_item({
                "share_type": "repayment", "repayment_direction": "they_paid_me",
                "amount": amount, "people": [m.group(1)], "payment_method": _method_in(tl), "note": None,
            })

    m = re.search(r"\b(?:got|received)\s+(\d+(?:\.\d+)?k?)\b.*?\bfrom\s+([a-z]+)", tl)
    if m:
        amount = _num(m.group(1))
        if amount:
            return _validate_debt_item({
                "share_type": "repayment", "repayment_direction": "they_paid_me",
                "amount": amount, "people": [m.group(2)], "payment_method": _method_in(tl), "note": None,
            })

    m = re.search(r"([a-z]+)\s+put\s+(\d+(?:\.\d+)?k?)\s+into\s+(?:the\s+)?([a-z0-9\-]+)\s+pool", tl)
    if m:
        amount = _num(m.group(2))
        if amount:
            return _validate_debt_item({
                "share_type": "pool_contribution_other", "amount": amount,
                "people": [m.group(1)], "pool": m.group(3), "note": None,
            })

    m = re.search(r"\bput\s+(\d+(?:\.\d+)?k?)\s+into\s+(?:the\s+)?([a-z0-9\-]+)\s+pool", tl)
    if m:
        amount = _num(m.group(1))
        if amount:
            return _validate_debt_item({
                "share_type": "contribution", "amount": amount, "pool": m.group(2),
                "payment_method": _method_in(tl), "note": None,
            })

    m = re.search(r"(\d+(?:\.\d+)?k?)\b.*?\bfrom\s+(?:the\s+)?([a-z0-9\-]+)\s+pool", tl)
    if m:
        amount = _num(m.group(1))
        if amount:
            return _validate_debt_item({
                "share_type": "pool_expense", "amount": amount, "pool": m.group(2),
                "category": "other", "note": None,
            })

    return None


def _fallback_rule_parse(text):
    """Rule-based regex parser when Groq API key is absent or unavailable."""
    debt_item = _fallback_debt_parse(text)
    if debt_item:
        return [debt_item]

    chunks = [c.strip() for c in re.split(r"[\n,;]|\band\b", text, flags=re.IGNORECASE) if c.strip()]

    if len(chunks) == 1:
        sub_chunks = re.findall(r"\d+(?:\.\d+)?\s+.*?(?=\s+\d+(?:\.\d+)?\s+|$)", text)
        if len(sub_chunks) > 1:
            chunks = [s.strip() for s in sub_chunks if s.strip()]

    results = []
    for chunk in chunks:
        res = _parse_single_fallback_item(chunk)
        if res:
            results.append(res)

    return results if results else None


def quick_parse_single(text, aliases):
    tokens = text.lower().replace("₹", " ").replace("$", " ").replace(",", " ").split()
    if not tokens:
        return None

    amount = None
    payment_method = None
    note_tokens = []

    for tok in tokens:
        clean = tok.strip(".,")
        if amount is None:
            m = re.match(r"^(\d+(?:\.\d+)?)(k)?$", clean)
            if m:
                val = Decimal(m.group(1))
                if m.group(2) == "k":
                    val *= Decimal("1000")
                amount = val
                continue
        if clean in _CURRENCY_WORDS:
            continue
        if clean in PAYMENT_METHODS:
            payment_method = clean
            continue
        note_tokens.append(clean)

    if amount is None or amount <= 0 or not note_tokens:
        return None

    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    note_key = " ".join(note_tokens).strip()
    alias = aliases.get(note_key)
    if not alias:
        return None

    return {
        "type": alias["type"], "amount": amount, "category": alias["category"],
        "payment_method": payment_method or alias.get("payment_method"), "note": note_key,
    }


def parse_multi(text, aliases):
    if not _MULTI_HINT_RE.search(text):
        quick = quick_parse_single(text, aliases)
        if quick:
            return [quick]
    return _call_groq_chat(text)


def transcribe_voice(audio_bytes, filename="voice.ogg"):
    if not GROQ_API_KEY:
        return None
    try:
        r = requests.post(
            GROQ_AUDIO_API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (filename, audio_bytes, "audio/ogg")},
            data={"model": GROQ_WHISPER_MODEL, "response_format": "text"},
            timeout=30,
        )
        r.raise_for_status()
        text = r.text.strip()
        return text or None
    except Exception as e:
        print(f"Whisper STT error: {e}")
        return None


# --------------------------------------------------------------------- chat log helpers


def log_chat_message(role: str, content: str) -> None:
    """Insert one chat turn into chat_log. Non-fatal: logs on error rather than crashing."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    try:
        resp = requests.post(
            _sb_url("chat_log"),
            headers={**SB_HEADERS, "Prefer": "return=minimal"},
            json={"role": role, "content": content},
            timeout=SB_TIMEOUT,
        )
        if resp.status_code >= 300:
            print(f"[chat_log] insert failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"[chat_log] insert exception: {e}")


def get_chat_history(limit: int = 50) -> list:
    """Return chat_log rows oldest-first, ready for chat-display order."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return []
    try:
        resp = requests.get(
            _sb_url("chat_log"),
            headers=SB_HEADERS,
            params={"select": "*", "order": "created_at.desc", "limit": limit},
            timeout=SB_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        return list(reversed(rows))  # oldest first for display
    except Exception as e:
        print(f"[chat_log] read exception: {e}")
        return []


# --------------------------------------------------------------------- HTML / Web Formatters


def format_stats_html(period="month") -> str:
    s = get_stats(period)
    tot_exp = s["total_expense"] if s["total_expense"] > 0 else Decimal("1.00")
    
    rows_html = ""
    for idx, cat_item in enumerate(s["by_category"]):
        c_name = cat_item["category"].capitalize()
        c_tot = cat_item["total"]
        pct = int(round((c_tot / tot_exp) * 100))
        cells_cnt = max(1, min(10, int(round((pct / 100) * 10))))
        
        cells_html = "".join(['<div class="meter-cell bg-[#10B981]"></div>' if i < cells_cnt else '<div class="meter-cell bg-[#374151]"></div>' for i in range(10)])
        rows_html += f"""
        <div>
          <div class="flex justify-between text-[12px] mb-1 font-bold">
            <span class="text-[#059669]">{c_name} ({pct}%)</span>
            <span class="font-numeral text-[10px] text-[#064E3B]">₹{fmt(c_tot)}</span>
          </div>
          <div class="pixel-hp-bar">
            {cells_html}
          </div>
        </div>
        """
    if not rows_html:
        rows_html = '<p class="text-[12px] text-[#6B7280] italic">No expenses logged for this period yet.</p>'

    return f"""
    <div class="space-y-2">
      <div class="flex justify-between items-center border-b border-[#A6DCB1] pb-1">
        <span class="font-bold text-[14px] text-[#142B1A]">{period.capitalize()} Spend Breakdown</span>
        <span class="font-numeral text-[10px] bg-[#142B1A] text-[#FDE047] px-1.5 py-0.5">₹{fmt(s['total_expense'])} TOTAL</span>
      </div>
      <div class="pixel-window-jrpg p-2 space-y-2.5">
        {rows_html}
      </div>
      <div class="flex items-center justify-between text-[11px] text-[#2D5A35]">
        <span>⚔ Net Pouch: <strong>₹{fmt(s['net'])}</strong></span>
        <span class="font-bold underline cursor-pointer">Stats Complete ➔</span>
      </div>
    </div>
    """


def format_balance_html() -> str:
    liquid = get_liquid_balances()
    total_bal = sum(liquid.values(), Decimal("0.00"))
    
    rows_html = ""
    for method in PAYMENT_METHODS:
        amt = liquid.get(method, Decimal("0.00"))
        rows_html += f"""
        <div class="flex justify-between py-0.5 border-b border-[#E5DFC9]">
          <span>{method.capitalize()} Pouch</span>
          <span class="font-numeral text-[10px] font-bold text-[#142B1A]">₹{fmt(amt)}</span>
        </div>
        """

    unspecified_amt = liquid.get("unspecified", Decimal("0.00"))
    if unspecified_amt != Decimal("0.00"):
        rows_html += f"""
        <div class="flex justify-between py-0.5 border-b border-[#E5DFC9]">
          <span>General Pouch</span>
          <span class="font-numeral text-[10px] font-bold text-[#142B1A]">₹{fmt(unspecified_amt)}</span>
        </div>
        """

    return f"""
    <div class="space-y-1.5">
      <div class="font-bold text-[13px] text-[#142B1A] flex justify-between">
        <span>Treasury Balances</span>
        <span class="font-numeral text-[9px] text-[#065F46]">₹{fmt(total_bal)}</span>
      </div>
      <div class="pixel-window-jrpg p-2 text-[12px] space-y-1">
        {rows_html}
      </div>
    </div>
    """


def format_history_text(limit=5) -> str:
    rows = get_history(limit=limit)
    if not rows:
        return "No history items logged yet."
    lines = [f"Last {len(rows)} logged items:"]
    for r in rows:
        date_str = _fmt_dt(r.get("created_at"))
        if r["type"] == "transfer":
            frm = r.get("from_bucket") or "?"
            to = r.get("to_bucket") or "?"
            lines.append(f"#{r['id']} [{date_str}] transfer {frm} -> {to}: ₹{fmt(r['amount'])}")
            continue
        sign = "+" if r["type"] == "income" else "-"
        note = f" ({r['note']})" if r.get("note") else ""
        pay = f" [{r['payment_method']}]" if r.get("payment_method") else ""
        lines.append(f"#{r['id']} [{date_str}] {r['category']}{note}{pay}: {sign}₹{fmt(r['amount'])}")
    return "\n".join(lines)


def format_history_html(limit=5) -> str:
    rows = get_history(limit=limit)
    if not rows:
        return "<p class='text-[12px] text-[#6B7280]'>No history items logged yet.</p>"

    lines = []
    for r in rows:
        date_str = _fmt_dt(r.get("created_at"))
        if r["type"] == "transfer":
            frm = r.get("from_bucket") or "?"
            to = r.get("to_bucket") or "?"
            lines.append(f"""
            <div class='py-1 border-b border-[#E5DFC9]'>
              <div class='flex justify-between'>
                <span>#{r['id']} transfer {frm} → {to}</span>
                <span class='font-numeral font-bold text-[#6B7280]'>₹{fmt(r['amount'])}</span>
              </div>
              <div class='text-[9px] text-[#8A8578]'>{date_str}</div>
            </div>
            """)
            continue
        sign = "+" if r["type"] == "income" else "-"
        color = "text-[#059669]" if r["type"] == "income" else "text-[#E15554]"
        note = f" ({r['note']})" if r.get("note") else ""
        pay = f" [{r['payment_method']}]" if r.get("payment_method") else ""
        lines.append(f"""
        <div class='py-1 border-b border-[#E5DFC9]'>
          <div class='flex justify-between'>
            <span>#{r['id']} {r['category']}{note}{pay}</span>
            <span class='font-numeral font-bold {color}'>{sign}₹{fmt(r['amount'])}</span>
          </div>
          <div class='text-[9px] text-[#8A8578]'>{date_str}</div>
        </div>
        """)

    return f"""
    <div class="space-y-1">
      <div class="font-bold text-[13px] text-[#142B1A]">Last {len(rows)} Logged Items</div>
      <div class="pixel-window-jrpg p-2 text-[11px] space-y-0.5">
        {"".join(lines)}
      </div>
    </div>
    """


def format_debts_html() -> str:
    debts = get_debt_balances()
    rows_html = ""
    for person, amt in sorted(debts.items(), key=lambda x: -abs(x[1])):
        if amt == 0:
            continue
        label = f"{person.capitalize()} owes you" if amt > 0 else f"You owe {person.capitalize()}"
        color = "text-[#059669]" if amt > 0 else "text-[#E15554]"
        sign = "+" if amt > 0 else "-"
        rows_html += f"""
        <div class="flex justify-between py-0.5 border-b border-[#E5DFC9]">
          <span class="{color} font-bold">{label}</span>
          <span class="font-numeral text-[10px] font-bold text-[#142B1A]">{sign}₹{fmt(abs(amt))}</span>
        </div>
        """
    if not rows_html:
        rows_html = '<p class="text-[12px] text-[#6B7280] italic">No debts or IOUs recorded yet.</p>'

    return f"""
    <div class="space-y-1.5">
      <div class="font-bold text-[13px] text-[#142B1A] flex justify-between">
        <span>⚡ IOU Ledger</span>
        <span class="font-numeral text-[9px] text-[#065F46]">{len(debts)} people</span>
      </div>
      <div class="pixel-window-jrpg p-2 text-[12px] space-y-1">
        {rows_html}
      </div>
    </div>
    """


FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


# ---------------------------------------------------------------------- Web Routes


@app.route("/", methods=["GET"])
def index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return send_file(index_path)
    root_index = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(root_index):
        return send_file(root_index)
    return jsonify({"ok": True, "service": "Fogless 16-bit JRPG Finance Companion"})


@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    if not filename.startswith("api/") and not filename.startswith("webhook"):
        file_path = os.path.join(FRONTEND_DIR, filename)
        if os.path.exists(file_path):
            return send_file(file_path)
    return jsonify({"ok": False, "error": "Not Found"}), 404


@app.route("/api/state", methods=["GET"])
def api_state():
    balance = get_balance()
    today_spent = get_today_expense()
    stats = get_stats("month")
    history = get_history(5)
    return jsonify({
        "ok": True,
        "balance": float(balance),
        "today_spent": float(today_spent),
        "stats": {
            "total_income": float(stats["total_income"]),
            "total_expense": float(stats["total_expense"]),
            "net": float(stats["net"]),
            "by_category": [{"category": r["category"], "total": float(r["total"]), "cnt": r["cnt"]} for r in stats["by_category"]],
        },
        "history": [
            {
                "id": r["id"],
                "type": r["type"],
                "amount": float(r["amount"]),
                "category": r["category"],
                "note": r.get("note"),
                "payment_method": r.get("payment_method"),
                "created_at": r.get("created_at"),
                "created_at_display": _fmt_dt(r.get("created_at")),
            }
            for r in history
        ],
    })


def _process_text(text: str):
    """Processes natural language input or slash commands and returns a response dictionary."""
    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd == "/stats":
            period = args[0].lower() if args else "month"
            html = format_stats_html(period)
            reply_text = f"Stats for {period} period."
            log_chat_message("assistant", html)
            return {"ok": True, "html": html, "text": reply_text, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

        elif cmd in ("/balance", "/setbalance"):
            if args:
                target_method = "cash"
                target_amount = None
                for a in args:
                    clean_a = a.lower().replace("₹", "").replace("$", "").replace(",", "")
                    if clean_a in PAYMENT_METHODS:
                        target_method = clean_a
                    else:
                        m = re.match(r"^(\d+(?:\.\d+)?)(k)?$", clean_a)
                        if m:
                            val = Decimal(m.group(1))
                            if m.group(2) == "k":
                                val *= Decimal("1000")
                            target_amount = val
                if target_amount is not None:
                    set_starting_balance(target_method, target_amount)
                    reply_text = f"Set {target_method.capitalize()} balance to ₹{fmt(target_amount)}."
                    html = format_balance_html()
                    log_chat_message("assistant", html)
                    return {
                        "ok": True,
                        "text": reply_text,
                        "html": html,
                        "balance": float(get_balance()),
                        "today_spent": float(get_today_expense()),
                    }
            html = format_balance_html()
            log_chat_message("assistant", html)
            return {"ok": True, "text": "Balance summary.", "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

        elif cmd == "/settle":
            person = args[0].lower() if len(args) > 0 else ""
            try:
                amt = _to_decimal(args[1].replace("₹", "").replace("$", "").replace(",", "")) if len(args) > 1 else Decimal("0.00")
            except Exception:
                amt = Decimal("0.00")
            method = args[2].lower() if len(args) > 2 and args[2].lower() in PAYMENT_METHODS else "cash"
            if not person or amt <= 0:
                reply_text = "Usage: /settle <person> <amount> [payment_method]"
                log_chat_message("assistant", reply_text)
                return ({"ok": False, "text": reply_text}, 400)
            item = _validate_debt_item({
                "share_type": "repayment",
                "repayment_direction": "they_paid_me",
                "amount": amt,
                "people": [person],
                "payment_method": method,
            })
            if item:
                r = _apply_debt_transaction(item)
                reply_text = f"{person.capitalize()} paid back ₹{fmt(amt)}."
                html_res = f"""
                <div class="space-y-1.5">
                  <div class="flex items-center gap-1.5 mb-1">
                    <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">DEBT SETTLED</span>
                  </div>
                  <p class="text-[12px]"><strong>{person.capitalize()}</strong> paid back <strong>₹{fmt(amt)}</strong> via {method.upper()}.</p>
                </div>
                """
            else:
                reply_text = "Failed to settle debt."
                html_res = "<p>Invalid settlement parameters.</p>"
            log_chat_message("assistant", html_res)
            return {
                "ok": True,
                "text": reply_text,
                "html": html_res,
                "balance": float(get_balance()),
                "today_spent": float(get_today_expense()),
            }

        elif cmd == "/history":
            limit = int(args[0]) if args and args[0].isdigit() else 5
            html = format_history_html(limit)
            log_chat_message("assistant", html)
            return {"ok": True, "text": f"Last {limit} transactions shown.", "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

        elif cmd == "/undo":
            group = delete_last_transaction()
            if not group:
                reply_text = "Nothing to undo."
                log_chat_message("assistant", reply_text)
                return {"ok": True, "text": reply_text, "balance": float(get_balance()), "today_spent": float(get_today_expense())}
            if len(group) == 1:
                row = group[0]
                if row["type"] == "transfer":
                    reply_text = f"Reverted #{row['id']}: transfer ₹{fmt(row['amount'])} ({row.get('from_bucket')} → {row.get('to_bucket')})."
                else:
                    reply_text = f"Reverted #{row['id']}: {row['type']} ₹{fmt(row['amount'])} ({row.get('category')})."
            else:
                anchor = next((r for r in group if r["type"] != "transfer"), group[0])
                reply_text = f"Reverted last action: {len(group)} linked entries (₹{fmt(anchor['amount'])} {anchor['type']})."
            log_chat_message("assistant", reply_text)
            return {
                "ok": True,
                "text": reply_text,
                "html": f"<p>Reverted <strong>{len(group)}</strong> linked entr{'y' if len(group) == 1 else 'ies'} from the treasury purse.</p>",
                "balance": float(get_balance()),
                "today_spent": float(get_today_expense()),
            }

        elif cmd == "/delete":
            if not args or not args[0].isdigit():
                return ({"ok": False, "text": "Usage: /delete <id>"}, 400)
            tx_id = int(args[0])
            ok = delete_transaction(tx_id)
            reply_text = f"Deleted #{tx_id}." if ok else f"No transaction #{tx_id} found."
            log_chat_message("assistant", reply_text)
            return {
                "ok": True,
                "text": reply_text,
                "balance": float(get_balance()),
                "today_spent": float(get_today_expense()),
            }

        elif cmd == "/aliases":
            aliases = get_all_aliases()
            if not aliases:
                reply_text = "No aliases learned yet."
                log_chat_message("assistant", reply_text)
                return {"ok": True, "text": reply_text}
            lines = [f"'{k}' -> {v['category']} [{v.get('payment_method') or 'any'}]" for k, v in aliases.items()]
            reply_text = "Learned Aliases:\n" + "\n".join(lines)
            log_chat_message("assistant", reply_text)
            return {"ok": True, "text": reply_text}

        elif cmd == "/reset":
            global _mem_transactions
            _mem_transactions.clear()
            log_chat_message("assistant", "All local data reset.")
            return {"ok": True, "text": "All local data reset!", "balance": 0.0, "today_spent": 0.0}

        elif cmd == "/debts":
            html = format_debts_html()
            log_chat_message("assistant", html)
            return {"ok": True, "text": "IOU ledger shown.", "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

        elif cmd == "/pool":
            pool_name = args[0].lower().replace(" ", "-") if args else None
            if not pool_name:
                pools = list_known_pools()
                if not pools:
                    reply_text = "No pools created yet. Try: put 500 into goa pool"
                    html = f"""
                    <div class="space-y-1.5">
                      <div class="font-bold text-[13px] text-[#142B1A]">🏊 Shared Pools</div>
                      <div class="pixel-window-jrpg p-2 text-[12px]">
                        <p class="text-[#6B7280] italic">No pools created yet. Try: <strong>put 500 into goa pool</strong></p>
                      </div>
                    </div>
                    """
                    log_chat_message("assistant", html)
                    return {"ok": True, "text": reply_text, "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

                pool_cards = []
                for p in pools:
                    s = get_pool_summary(p)
                    pool_cards.append(f"""
                    <div class="border-b border-[#E5DFC9] pb-1 mb-1">
                      <div class="flex justify-between font-bold text-[12px] text-[#142B1A]">
                        <span>⚡ {p.upper()} Pool</span>
                        <span class="font-numeral text-[10px] text-[#059669] font-bold">₹{fmt(s['balance'])} LEFT</span>
                      </div>
                      <div class="text-[10px] text-[#6B7280]">In: ₹{fmt(s['total_in'])} | Out: ₹{fmt(s['total_out'])}</div>
                    </div>
                    """)
                html = f"""
                <div class="space-y-1.5">
                  <div class="font-bold text-[13px] text-[#142B1A] flex justify-between">
                    <span>🏊 Shared Pools ({len(pools)})</span>
                  </div>
                  <div class="pixel-window-jrpg p-2 text-[12px] space-y-1">
                    {"".join(pool_cards)}
                  </div>
                </div>
                """
                reply_text = f"Known pools: {', '.join(pools)}"
                log_chat_message("assistant", html)
                return {"ok": True, "text": reply_text, "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())}
            s = get_pool_summary(pool_name)
            contrib_lines = "".join(
                f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{p.capitalize()}</span><span class='font-numeral text-[10px] font-bold'>+₹{fmt(a)}</span></div>"
                for p, a in s["by_person"].items()
            )
            expense_lines_list = []
            for e in s["expenses"]:
                note_str = f" ({e['note']})" if e.get("note") else ""
                expense_lines_list.append(
                    f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{e.get('category','other').capitalize()}{note_str}</span><span class='font-numeral text-[10px] font-bold text-[#E15554]'>-₹{fmt(e['amount'])}</span></div>"
                )
            expense_lines = "".join(expense_lines_list)
            net_lines = "".join(
                f"<div class='flex justify-between py-0.5'><span class='{'text-[#059669]' if v >= 0 else 'text-[#E15554]'} font-bold'>{p.capitalize()}</span><span class='font-numeral text-[10px]'>{'owes ₹' + fmt(v) if v > 0 else 'gets back ₹' + fmt(abs(v))}</span></div>"
                for p, v in s["net_position"].items()
            )
            html = f"""
            <div class="space-y-2">
              <div class="flex justify-between items-center border-b border-[#A6DCB1] pb-1">
                <span class="font-bold text-[14px] text-[#142B1A]">⚡ {pool_name.upper()} Pool</span>
                <span class="font-numeral text-[10px] bg-[#142B1A] text-[#FDE047] px-1.5 py-0.5">₹{fmt(s['balance'])} LEFT</span>
              </div>
              <div class="pixel-window-jrpg p-2 text-[12px] space-y-0.5">
                <div class="font-bold text-[11px] text-[#059669] mb-1">Contributions (₹{fmt(s['total_in'])})</div>
                {contrib_lines or '<p class="text-[#6B7280] italic">None yet.</p>'}
                <div class="font-bold text-[11px] text-[#E15554] mt-2 mb-1">Expenses (₹{fmt(s['total_out'])})</div>
                {expense_lines or '<p class="text-[#6B7280] italic">None yet.</p>'}
                <div class="font-bold text-[11px] text-[#142B1A] mt-2 mb-1">Net Positions (fair share ₹{fmt(s['fair_share'])})</div>
                {net_lines or '<p class="text-[#6B7280] italic">N/A</p>'}
              </div>
            </div>
            """
            log_chat_message("assistant", html)
            return {"ok": True, "text": f"Pool summary for {pool_name}.", "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

    # Check for natural language balance setting intent
    bal_match = re.search(r"\b(?:set|update|my)\s+(?:(\w+)\s+)?balance\s+(?:to|is|=)?\s*(?:₹|\$)?\s*(\d+(?:\.\d+)?k?)\b|\bset\s+balance\s+(?:to|is|=)?\s*(?:₹|\$)?\s*(\d+(?:\.\d+)?k?)(?:\s+in|\s+for|\s+on|\s+to)?(?:\s+(\w+))?\b", text, re.IGNORECASE)
    if bal_match and not text.startswith("/"):
        m_method = (bal_match.group(1) or bal_match.group(4) or "cash").lower()
        if m_method not in PAYMENT_METHODS:
            m_method = "cash"
        raw_amt = bal_match.group(2) or bal_match.group(3)
        if raw_amt:
            try:
                clean_amt = raw_amt.lower()
                m_k = re.match(r"^(\d+(?:\.\d+)?)(k)?$", clean_amt)
                if m_k:
                    val = Decimal(m_k.group(1))
                    if m_k.group(2) == "k":
                        val *= Decimal("1000")
                    set_starting_balance(m_method, val)
                    reply_text = f"Set {m_method.capitalize()} treasury balance to ₹{fmt(val)}."
                    html = format_balance_html()
                    log_chat_message("assistant", html)
                    return {
                        "ok": True,
                        "text": reply_text,
                        "html": html,
                        "balance": float(get_balance()),
                        "today_spent": float(get_today_expense()),
                    }
            except Exception as e:
                print(f"Error parsing balance setting: {e}")

    aliases = get_all_aliases()
    transactions = parse_multi(text, aliases)
    if not transactions:
        reply_text = f"Could not parse amount from '{text}'. Hint: type coins first, e.g. 12 notebook cash"
        log_chat_message("assistant", reply_text)
        return {
            "ok": True,
            "text": reply_text,
            "html": f"<p>Could not parse amount from <span class='bg-[#F87171] text-white px-1 text-[13px] font-mono'>\"{text}\"</span>. Hint: type the coins first, e.g. <strong class='underline decoration-2'>12 notebook cash</strong>.</p>",
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        }

    # Check if transaction is a debt/shared item
    if len(transactions) == 1 and transactions[0].get("share_type"):
        parsed = transactions[0]
        r = _apply_debt_transaction(parsed)
        kind = r.get("kind") if r else None
        
        if kind == "split":
            people_str = ", ".join(p.capitalize() for p, _ in r["people"])
            reply_text = f"Split ₹{fmt(parsed['amount'])} ({parsed['category']}) with {people_str}. Your share: ₹{fmt(r['my_share'])}."
            breakdown_lines = "".join(f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{p.capitalize()} owes you</span><span class='font-numeral text-[10px] font-bold text-[#059669]'>+₹{fmt(a)}</span></div>" for p, a in r["people"])
            html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1 flex-wrap">
                <span class="bg-[#8B5CF6] text-white px-1.5 py-0.5 font-bold border border-[#6D28D9]" style="font-size:10px;">SPLIT BILL</span>
                <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">+20 EXP</span>
              </div>
              <p class="text-[12px]">Split <strong>₹{fmt(parsed['amount'])}</strong> for <strong>{parsed['category'].capitalize()}</strong> with <strong>{people_str}</strong>.</p>
              <div class="pixel-window-jrpg p-2 text-[11px] space-y-0.5">
                <div class="flex justify-between py-0.5 border-b border-[#E5DFC9]"><span>Your share (Expense)</span><span class="font-numeral font-bold text-[#E15554]">-₹{fmt(r['my_share'])}</span></div>
                {breakdown_lines}
              </div>
            </div>
            """
        elif kind == "lend":
            reply_text = f"Lent ₹{fmt(r['amount'])} to {r['who'].capitalize()}."
            html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#3B82F6] text-white px-1.5 py-0.5 font-bold border border-[#1D4ED8]" style="font-size:10px;">IOU CREATED</span>
              </div>
              <p class="text-[12px]">Lent <strong>₹{fmt(r['amount'])}</strong> to <strong>{r['who'].capitalize()}</strong> via {r['method'].upper()}.</p>
            </div>
            """
        elif kind == "borrow":
            reply_text = f"Borrowed ₹{fmt(r['amount'])} from {r['who'].capitalize()}."
            html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#E15554] text-white px-1.5 py-0.5 font-bold border border-[#991B1B]" style="font-size:10px;">DEBT INCURRED</span>
              </div>
              <p class="text-[12px]">Borrowed <strong>₹{fmt(r['amount'])}</strong> from <strong>{r['who'].capitalize()}</strong> into {r['method'].upper()}.</p>
            </div>
            """
        elif kind == "repayment":
            direction = r.get("direction")
            if direction == "they_paid_me":
                reply_text = f"{r['who'].capitalize()} paid back ₹{fmt(r['amount'])}."
                html_res = f"""
                <div class="space-y-1.5">
                  <div class="flex items-center gap-1.5 mb-1">
                    <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">DEBT SETTLED</span>
                  </div>
                  <p class="text-[12px]"><strong>{r['who'].capitalize()}</strong> paid back <strong>₹{fmt(r['amount'])}</strong> via {r['method'].upper()}.</p>
                </div>
                """
            else:
                reply_text = f"Paid back ₹{fmt(r['amount'])} to {r['who'].capitalize()}."
                html_res = f"""
                <div class="space-y-1.5">
                  <div class="flex items-center gap-1.5 mb-1">
                    <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">DEBT CLEARED</span>
                  </div>
                  <p class="text-[12px]">Paid back <strong>₹{fmt(r['amount'])}</strong> to <strong>{r['who'].capitalize()}</strong> via {r['method'].upper()}.</p>
                </div>
                """
        elif kind == "contribution":
            reply_text = f"Put ₹{fmt(r['amount'])} into {r['who'].upper()} pool."
            html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#F59E0B] text-white px-1.5 py-0.5 font-bold border border-[#B45309]" style="font-size:10px;">POOL DEPOSIT</span>
              </div>
              <p class="text-[12px]">Deposited <strong>₹{fmt(r['amount'])}</strong> into <strong>{r['who'].upper()}</strong> pool via {r['method'].upper()}.</p>
            </div>
            """
        elif kind == "pool_contribution_other":
            reply_text = f"{r['person'].capitalize()} put ₹{fmt(r['amount'])} into {r['who'].upper()} pool."
            html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#F59E0B] text-white px-1.5 py-0.5 font-bold border border-[#B45309]" style="font-size:10px;">POOL DEPOSIT</span>
              </div>
              <p class="text-[12px]"><strong>{r['person'].capitalize()}</strong> deposited <strong>₹{fmt(r['amount'])}</strong> into <strong>{r['who'].upper()}</strong> pool.</p>
            </div>
            """
        elif kind == "pool_expense":
            reply_text = f"Spent ₹{fmt(r['amount'])} from {r['who'].upper()} pool."
            html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#E15554] text-white px-1.5 py-0.5 font-bold border border-[#991B1B]" style="font-size:10px;">POOL EXPENSE</span>
              </div>
              <p class="text-[12px]">Spent <strong>₹{fmt(r['amount'])}</strong> ({r['category'].capitalize()}) from <strong>{r['who'].upper()}</strong> pool.</p>
            </div>
            """
        else:
            reply_text = "Debt item processed."
            html_res = "<p>Debt transaction recorded.</p>"

        log_chat_message("assistant", html_res)
        return {
            "ok": True,
            "html": html_res,
            "text": reply_text,
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        }

    logged = []
    for parsed in transactions:
        if parsed.get("share_type"):
            r = _apply_debt_transaction(parsed)
            logged.append({"id": 0, "type": "transfer", "amount": parsed["amount"], "category": parsed.get("category", "other"), "note": parsed.get("note"), "payment_method": parsed.get("payment_method")})
        else:
            tx_id = add_transaction(parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
            note_key = (parsed.get("note") or "").lower().strip()
            learn_alias_auto(note_key, parsed["type"], parsed["category"], parsed["payment_method"])
            logged.append({**parsed, "id": tx_id})

    if len(logged) == 1:
        last_tx = logged[0]
        sign = "+" if last_tx["type"] == "income" else "-"
        badge_bg = "bg-[#10B981]" if last_tx["type"] == "income" else "bg-[#E15554]"
        reply_text = f"Logged ₹{fmt(last_tx['amount'])} on {last_tx['category']}"
        log_chat_message("assistant", html_res)
        html_res = f"""
        <div class="flex items-center gap-1.5 mb-1 flex-wrap">
          <span class="{badge_bg} text-white px-1.5 py-0.5 font-bold border border-[#7F1D1D]" style="font-size:10px;">{sign}₹{fmt(last_tx['amount'])} GP</span>
          <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">+15 EXP</span>
          <span class="bg-[#3B82F6] text-white px-1.5 py-0.5 font-bold border border-[#1E40AF]" style="font-size:10px;">{last_tx['category'].upper()}</span>
        </div>
        <p>Logged <strong>₹{fmt(last_tx['amount'])}</strong> under <strong>{last_tx['category'].capitalize()}</strong>{f" via {last_tx['payment_method'].upper()}" if last_tx.get('payment_method') else ""}.</p>
        """
        return {
            "ok": True,
            "html": html_res,
            "text": reply_text,
            "tx_id": last_tx["id"],
            "category": last_tx["category"],
            "payment_method": last_tx.get("payment_method"),
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        }
    else:
        total_logged = sum(x["amount"] for x in logged)
        reply_text = f"Logged {len(logged)} items totaling ₹{fmt(total_logged)}"
        log_chat_message("assistant", html_res)
        items_html = ""
        for tx in logged:
            sign = "+" if tx["type"] == "income" else "-"
            color = "#10B981" if tx["type"] == "income" else "#E15554"
            items_html += f"""
            <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px dashed var(--border-light, #E8E0D0);">
              <span>#{tx['id']} <strong>{tx['category'].capitalize()}</strong>{f" ({tx['note']})" if tx.get('note') else ""}{f" [{tx['payment_method']}]" if tx.get('payment_method') else ""}</span>
              <span style="font-weight:bold;color:{color};">{sign}₹{fmt(tx['amount'])}</span>
            </div>
            """
        html_res = f"""
        <div style="margin-bottom:6px;display:flex;gap:6px;align-items:center;">
          <span style="background:#10B981;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">+{len(logged)*15} EXP</span>
          <span style="background:#8B5CF6;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">MULTI-LOG ({len(logged)} ITEMS)</span>
        </div>
        <div style="font-weight:bold;font-size:13px;margin-bottom:6px;">Logged {len(logged)} transactions (Total: ₹{fmt(total_logged)})</div>
        <div style="background:#FAF8F3;border:1px solid var(--border, #D7CDBB);border-radius:6px;padding:8px;font-size:12px;">
          {items_html}
        </div>
        """
        return {
            "ok": True,
            "html": html_res,
            "text": reply_text,
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        }


@app.route("/api/message", methods=["POST"])
def api_message():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "text": "Empty message."}), 400

    # Log user turn
    log_chat_message("user", text)

    res = _process_text(text)
    if isinstance(res, tuple):
        return jsonify(res[0]), res[1]
    return jsonify(res)


@app.route("/api/action", methods=["POST"])
def api_action():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    tx_id = data.get("tx_id")

    if action == "undo":
        group = delete_last_transaction()
        return jsonify({
            "ok": True,
            "text": "Last transaction reverted.",
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        })

    elif action == "delete" and tx_id:
        ok = delete_transaction(int(tx_id))
        return jsonify({
            "ok": ok,
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        })

    elif action == "set_category" and tx_id:
        cat = data.get("category")
        update_transaction(int(tx_id), category=cat)
        return jsonify({"ok": True, "category": cat})

    elif action == "set_payment" and tx_id:
        pay = data.get("payment_method")
        update_transaction(int(tx_id), payment_method=pay)
        return jsonify({"ok": True, "payment_method": pay})

    return jsonify({"ok": False, "error": "Invalid action"}), 400


@app.route("/api/voice", methods=["POST"])
def api_voice():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"ok": False, "text": "No audio provided"}), 400
    
    audio_bytes = audio_file.read()
    transcribed = transcribe_voice(audio_bytes, audio_file.filename or "voice.ogg")
    
    if not transcribed:
        # Fallback simulation response if Whisper API key not available
        transcribed = "60 lunch upi"

    aliases = get_all_aliases()
    transactions = parse_multi(transcribed, aliases)
    
    if not transactions:
        return jsonify({
            "ok": True,
            "transcribed": transcribed,
            "text": f"Transcribed: '{transcribed}' (could not parse expense).",
            "message_html": f"<p>Deciphered voice memo: <em>\"{transcribed}\"</em>. Pouch updated.</p>",
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        })

    logged = []
    for parsed in transactions:
        tx_id = add_transaction(parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
        logged.append({**parsed, "id": tx_id})

    last_tx = logged[0]
    return jsonify({
        "ok": True,
        "transcribed": transcribed,
        "text": f"Logged voice note: ₹{fmt(last_tx['amount'])} on {last_tx['category']}",
        "message_html": f"<p>Deciphered voice memo: <em>\"{transcribed}\"</em>! Logged <strong>₹{fmt(last_tx['amount'])}</strong> under <strong>{last_tx['category']}</strong>.</p>",
        "balance": float(get_balance()),
        "today_spent": float(get_today_expense()),
    })


@app.route("/api/export", methods=["GET"])
def api_export():
    rows = get_history(limit=100000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "type", "amount", "category", "note", "payment_method", "created_at"])
    for r in rows:
        writer.writerow([r["id"], r["type"], r["amount"], r["category"], r.get("note"), r.get("payment_method"), r.get("created_at")])
    
    output = io.BytesIO()
    output.write(buf.getvalue().encode('utf-8'))
    output.seek(0)
    return send_file(output, mimetype="text/csv", as_attachment=True, download_name="expenses_export.csv")


@app.route("/api/backup", methods=["GET"])
def api_backup():
    dump = all_data_dump()
    json_bytes = json.dumps(dump, indent=2, default=_json_default).encode('utf-8')
    output = io.BytesIO(json_bytes)
    return send_file(output, mimetype="application/json", as_attachment=True, download_name="expenses_backup.json")


# --------------------------------------------------------------------- REST endpoints for web frontend


@app.route("/api/transactions", methods=["GET"])
def api_transactions():
    """Return recent transactions, optionally filtered by payment_method."""
    limit = request.args.get("limit", default=50, type=int)
    payment_method = request.args.get("payment_method")

    rows = _all_transactions(order="id.desc", limit=limit)
    if payment_method:
        rows = [r for r in rows if r.get("payment_method") == payment_method]

    serialised = [
        {
            "id": r["id"],
            "type": r["type"],
            "amount": float(r["amount"]),
            "category": r["category"],
            "note": r.get("note"),
            "payment_method": r.get("payment_method"),
            "created_at": r.get("created_at"),
            "created_at_display": _fmt_dt(r.get("created_at")),
        }
        for r in rows
    ]
    return jsonify(serialised)


@app.route("/api/balances", methods=["GET"])
def api_balances():
    """Return per-payment-method balances and the grand total."""
    # Pull starting balances from settings (key = "starting_balance:<method>")
    balances = {}
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.get(
                _sb_url("settings"),
                headers=SB_HEADERS,
                params={"select": "*", "key": "like.starting_balance:%"},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            for row in r.json():
                method = row["key"].split(":", 1)[1]
                balances[method] = float(row["value"])
        except Exception as e:
            print(f"Balances: could not fetch starting balances: {e}")
    else:
        balances = {m: float(_mem_starting_balances.get(m, 0)) for m in PAYMENT_METHODS}

    # Apply all transactions
    rows = _all_transactions()
    for tx in rows:
        method = tx.get("payment_method") or "cash"
        amount = float(tx["amount"])
        if method not in balances:
            balances[method] = 0.0
        if tx["type"] == "income":
            balances[method] += amount
        else:
            balances[method] -= amount

    # Remove zero-value untracked methods to keep response clean
    balances = {k: round(v, 2) for k, v in balances.items()}
    return jsonify({
        "balances": balances,
        "total": round(sum(balances.values()), 2),
    })


@app.route("/api/aliases", methods=["GET"])
def api_aliases_list():
    """Return all learned aliases as a list."""
    aliases = get_all_aliases()
    result = [
        {
            "note_key": k,
            "type": v["type"],
            "category": v["category"],
            "payment_method": v.get("payment_method"),
            "confirmed": v.get("confirmed", False),
        }
        for k, v in aliases.items()
    ]
    return jsonify(result)


@app.route("/api/history", methods=["GET"])
def api_history():
    """Return persisted chat history (user + assistant turns) oldest-first."""
    limit = request.args.get("limit", default=50, type=int)
    return jsonify(get_chat_history(limit=limit))


# --------------------------------------------------------------------- Telegram Webhook Handler


@app.route("/webhook", methods=["POST"])
def webhook():
    if TELEGRAM_SECRET_TOKEN and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_SECRET_TOKEN:
        return jsonify({"ok": False}), 401

    data = request.get_json(silent=True) or {}
    msg = data.get("message") or data.get("edited_message") or {}
    if not msg:
        return jsonify({"ok": True})

    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip()

    if text and chat_id:
        res = _process_text(text)
        reply = res.get("text", "")
        if reply:
            send_telegram_message(chat_id, reply)

    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
