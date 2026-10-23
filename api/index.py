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
from flask import Flask, request, jsonify, send_file, Response, render_template_string, session
import hashlib
import hmac
import time
import traceback
from html import escape as _esc, unescape as _unesc
from werkzeug.exceptions import HTTPException
from werkzeug.utils import safe_join


# --------------------------------------------------------------------- config


CRON_SECRET = os.getenv("CRON_SECRET", "")

# Demo login. Override with DEMO_USERNAME / DEMO_PASSWORD env vars if you ever want different ones.
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "vaibhav10")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "iwon10")


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))

# Signs the login cookie. Set SESSION_SECRET in production; without it we derive a stable secret
# from the Supabase key (so every serverless instance agrees), or a fixed one for local testing.
SESSION_SECRET = os.getenv("SESSION_SECRET") or (
    hashlib.sha256(("fogless-session:" + SUPABASE_KEY).encode("utf-8")).hexdigest()
    if SUPABASE_KEY else "fogless-local-demo-secret"
)


GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_CHAT_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO_API = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"


SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
} if SUPABASE_KEY else {}
SB_TIMEOUT = 8
SB_PAGE_SIZE = 1000


IST = timezone(timedelta(hours=5, minutes=30))  # local calendar-day boundary for /stats "today"

# Wiping the live database through /reset must be explicitly enabled (ALLOW_RESET=1).
ALLOW_RESET = os.getenv("ALLOW_RESET", "") == "1"


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
- A person's or place's NAME on its own is just the note of a normal expense. "rohan rahul 800 upi", "taj mahal 800 upi"
  and "800 for taj mahal upi" are each ONE plain expense of 800 paid by upi (note = the name). Use the SHARED & DEBT rules
  below ONLY when the message contains one of their trigger phrases (split with, gave/lent, borrowed from, paid back,
  from <person>, put into <pool> ...). Never guess a debt from a bare name.
- Keep a multi-word name together as ONE name ("rohan rahul" is one person or place, never "rohan" and "rahul").
- Several names joined by "and" with a SINGLE amount ("rohan rahul and taj mahal for 800 upi") are ONE expense of 800
  whose note is those names - not several transactions. Only a separate amount starts a new transaction.

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
app.secret_key = SESSION_SECRET
app.config.update(
    SESSION_COOKIE_NAME="fogless_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),  # https on Vercel; plain http works locally
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)


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


# ------------------------------------------------ amount / name hygiene helpers

MAX_AMOUNT = Decimal("1000000000")  # 100 crore: anything bigger is a typo, not a purchase
MAX_TEXT_LEN = 1000
_SELF_WORDS = {"me", "myself", "i", "self", "us", "mine"}
_NOT_PEOPLE = {"salary", "freelance", "refund", "bonus", "interest", "cashback", "job", "work"}
_NAME_STOP = {"via", "on", "using", "by", "for", "in", "at", "and", "back", "each", "way", "ways",
              "please", "today", "yesterday", "paid", "split", "share", "with", "from", "to", "the"}
_FILLER_WORDS = {"for", "on", "at", "to", "via", "using", "by", "in", "of", "the", "a", "an", "and",
                 "spent", "paid", "pay", "paying", "bought", "buy", "from", "with", "&",
                 "got", "received", "earned", "credited", "deposited"}
_KNOWN_COMMANDS = {"/stats", "/balance", "/setbalance", "/settle", "/history", "/undo", "/delete",
                   "/aliases", "/reset", "/debts", "/pool"}
_AMT_TOKEN_RE = re.compile(r"^(\d+(?:\.\d+)?)(k)?$", re.IGNORECASE)
_SYMBOL_RE = re.compile(r"[₹$]\s*")
_CUR_PREFIX_RE = re.compile(r"(?<![a-z])(?:rs\.?|inr)\s*(?=[-−–—]?\d)", re.IGNORECASE)
_CUR_SUFFIX_RE = re.compile(r"(?<=\d)\s*(?:/-|(?:rs\.?|inr|rupees?)(?![a-z]))", re.IGNORECASE)
_THOUSANDS_RE = re.compile(r"(?<![\d,.])\d{1,3}(?:,\d{2,3})*,\d{3}(?![\d,])(?:\.\d+)?")
_NEG_RE = re.compile(r"(?<![\w.])[-−–—](\d+(?:\.\d+)?k?)(?!\w)", re.IGNORECASE)


def _pn(s) -> str:
    """Display form of a name, plain text: 'rohan rahul' -> 'Rohan Rahul'."""
    return " ".join(w.capitalize() for w in str(s).split())


def _nm(s) -> str:
    """Same as _pn but HTML-escaped."""
    return _esc(_pn(s))


def _clean_name(raw, hyphen=False) -> str:
    """Lower-case, strip anything that is not a name character (this also makes the value safe to
    print inside HTML), collapse spaces. hyphen=True is for pool names ('goa trip' -> 'goa-trip')."""
    s = re.sub(r"[^a-z0-9 '.\-_]", "", str(raw or "").lower())
    s = " ".join(s.split()).strip(" .-_'")[:40]
    return s.replace(" ", "-") if hyphen else s


def _amount_from_str(s):
    """'1.5k' -> Decimal('1500'); None when s is not a plain amount."""
    m = _AMT_TOKEN_RE.match((s or "").strip())
    if not m:
        return None
    val = Decimal(m.group(1))
    if m.group(2):
        val *= Decimal("1000")
    return val


def _parse_amount_token(tok):
    """For slash commands. Returns (amount, was_negative); amount is None if tok is not an amount.
    A leading minus is reported (so the caller can tell the user), never silently applied."""
    t = (tok or "").strip().replace("₹", "").replace("$", "").replace(",", "")
    t = re.sub(r"^(?:rs\.?|inr)", "", t, flags=re.IGNORECASE)
    neg = bool(t) and t[0] in "-−–—"
    if neg:
        t = t[1:]
    return _amount_from_str(t), neg


def _normalize_amount_text(text):
    """Cleans the way people really type money, BEFORE any parser sees it:
    '₹500' / 'rs500' / '500rs' / '500/-' -> '500';  '1,200' / '1,20,000' -> '1200' / '120000'
    (otherwise the comma splits it into two fake items);  '-500' -> '500' and the fact that it WAS
    negative is returned, so the reply can say 'positive value converted'.
    Returns (clean_text, [Decimal amounts that were typed negative])."""
    t = _SYMBOL_RE.sub("", text or "")
    t = _CUR_PREFIX_RE.sub("", t)
    t = _CUR_SUFFIX_RE.sub("", t)
    t = _THOUSANDS_RE.sub(lambda m: m.group(0).replace(",", ""), t)
    negs = []

    def _strip_neg(m):
        val = _amount_from_str(m.group(1))
        if val is not None:
            negs.append(_to_decimal(val))
        return m.group(1)

    t = _NEG_RE.sub(_strip_neg, t)
    return re.sub(r"[ \t]+", " ", t).strip(), negs


def _flag_negatives(items, negs):
    """Marks parsed items whose amount was typed with a minus sign. Returns the notice lines."""
    pending = list(negs)
    notes = []
    for it in items or []:
        if it.get("amount") in pending:
            pending.remove(it["amount"])
            it["neg_converted"] = True
            notes.append(f"Negative amount -₹{fmt(it['amount'])} converted to positive ₹{fmt(it['amount'])}.")
    if pending and items and not notes:
        notes.append("A minus sign was ignored: amounts are always logged as positive values.")
    return notes


def _with_notice(resp, notes):
    """Appends warning lines to both the plain-text and the HTML reply."""
    if not notes:
        return resp
    joined = " ".join(notes)
    resp["text"] = (resp.get("text") or "") + "\n⚠ " + joined
    resp["html"] = (resp.get("html") or "") + (
        "<p style='margin-top:6px;font-size:11px;color:#B45309;'>⚠ " + _esc(joined) + "</p>")
    return resp


def _html_to_text(html) -> str:
    """Plain-text version of one of our HTML cards."""
    t = re.sub(r"</(?:div|p)>|<br\s*/?>", "\n", html or "")
    t = re.sub(r"</span>\s*<span[^>]*>", "  ", t)
    t = _unesc(re.sub(r"<[^>]+>", "", t))
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in t.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _take_name(s, max_words=4):
    """Leading run of name-like words ('rohan rahul upi' -> 'rohan rahul'). None if there is none."""
    words = []
    for tok in (s or "").lower().split():
        t = tok.strip(".,;:!?")
        if (not t or t in PAYMENT_METHODS or t in _NAME_STOP or t in _CURRENCY_WORDS
                or any(c.isdigit() for c in t)):
            break
        words.append(t)
        if len(words) >= max_words:
            break
    name = _clean_name(" ".join(words))
    return name if name and name not in _SELF_WORDS and name not in _NOT_PEOPLE else None


def _take_name_tail(s, max_words=3):
    """Trailing run of name-like words ('yesterday rohan rahul' -> 'rohan rahul')."""
    words = []
    for tok in reversed((s or "").lower().split()):
        t = tok.strip(".,;:!?")
        if (not t or t in PAYMENT_METHODS or t in _NAME_STOP or t in _CURRENCY_WORDS
                or any(c.isdigit() for c in t)):
            break
        words.insert(0, t)
        if len(words) >= max_words:
            break
    name = _clean_name(" ".join(words))
    return name if name and name not in _SELF_WORDS and name not in _NOT_PEOPLE else None


def _take_people(s):
    people = []
    for part in re.split(r"\s+and\s+|\s*,\s*|\s*&\s*", (s or "").strip()):
        n = _take_name(part)
        if n and n not in people:
            people.append(n)
    return people


def _leftover_note(s):
    """The words of s that are not an amount / payment method / filler -> short note, or None."""
    words = []
    for tok in (s or "").lower().split():
        t = tok.strip(".,;:")
        if (_amount_from_str(t) is not None or t in PAYMENT_METHODS or t in _CURRENCY_WORDS
                or not any(c.isalnum() for c in t)):
            continue
        words.append(t)
    while words and words[0] in _FILLER_WORDS:
        words.pop(0)
    while words and words[-1] in _FILLER_WORDS:
        words.pop()
    return " ".join(words)[:100] or None


# ---------------------------------------------------------------- data layer


class StorageError(Exception):
    """Supabase is configured but a call to it failed. We NEVER fall back to memory in that case:
    a silent fallback loses writes on serverless and shows an empty ledger on reads."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _use_sb() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _sb_url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}" if SUPABASE_URL else ""


def _sb(method, path, params=None, json_body=None, prefer=None, ok_statuses=()):
    """One place for every Supabase call. Raises StorageError on network errors and on any
    non-2xx/3xx status (except those listed in ok_statuses)."""
    headers = dict(SB_HEADERS)
    if prefer:
        headers["Prefer"] = prefer
    try:
        r = requests.request(
            method, _sb_url(path), headers=headers, params=params, json=json_body, timeout=SB_TIMEOUT
        )
    except requests.RequestException as e:
        print(f"[supabase] {method} {path} network error: {e}")
        raise StorageError(f"{method} {path}: {type(e).__name__}") from e
    if r.status_code >= 300 and r.status_code not in ok_statuses:
        print(f"[supabase] {method} {path} -> {r.status_code} {r.text[:300]}")
        raise StorageError(f"{method} {path} failed (HTTP {r.status_code})", r.status_code)
    return r


def _rows(r):
    try:
        data = r.json(parse_float=Decimal)
    except ValueError as e:
        raise StorageError("Supabase returned a non-JSON response") from e
    return data if isinstance(data, list) else []


def _first_id(r, what):
    rows = _rows(r)
    if not rows or "id" not in rows[0]:
        raise StorageError(f"Supabase insert into {what} returned no row")
    return rows[0]["id"]


# In-memory id counter that only exists for the no-Supabase (local testing) mode
_mem_pool_id_counter = 1


def add_transaction(tx_type, amount, category, note, payment_method=None) -> int:
    global _mem_id_counter
    amount_dec = _to_decimal(amount)

    if _use_sb():
        r = _sb(
            "POST", "transactions",
            json_body={
                "type": tx_type,
                "amount": str(amount_dec),
                "category": category,
                "note": note,
                "payment_method": payment_method,
            },
            prefer="return=representation",
        )
        tx_id = _first_id(r, "transactions")
        return tx_id

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
    (cash/upi/card/...) or virtual ones: iou:<person> for a debt, pool:<n> for your own stake
    in a shared pool. Never counted as income or expense. category is always 'transfer' so the
    NOT NULL constraint on transactions.category can never reject the row."""
    global _mem_id_counter
    amount_dec = _to_decimal(amount)

    if _use_sb():
        r = _sb(
            "POST", "transactions",
            json_body={
                "type": "transfer",
                "amount": str(amount_dec),
                "category": "transfer",
                "from_bucket": from_bucket,
                "to_bucket": to_bucket,
                "note": note,
                "person": person,
                "pool": pool,
                "linked_tx_id": linked_tx_id,
            },
            prefer="return=representation",
        )
        tx_id = _first_id(r, "transactions")
        return tx_id

    tx_id = _mem_id_counter
    _mem_id_counter += 1
    _mem_transactions.append({
        "id": tx_id,
        "type": "transfer",
        "amount": amount_dec,
        "category": "transfer",
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
        rows = [r for r in rows if r["created_at"] >= since]
    rows.sort(key=lambda x: x["id"], reverse=("desc" in order))
    if limit:
        rows = rows[:limit]
    return rows


def get_history(limit=10):
    return _all_transactions(limit=limit)


def get_transaction(tx_id):
    if _use_sb():
        rows = _rows(_sb("GET", "transactions", params={"select": "*", "id": f"eq.{tx_id}"}))
        return rows[0] if rows else None
    for r in _mem_transactions:
        if r["id"] == tx_id:
            return r
    return None


def update_transaction(tx_id, **fields) -> bool:
    if not fields:
        return False
    if _use_sb():
        r = _sb(
            "PATCH", "transactions",
            params={"id": f"eq.{tx_id}"},
            json_body=fields,
            prefer="return=representation",
        )
        return len(_rows(r)) > 0
    tx = get_transaction(tx_id)
    if tx:
        tx.update(fields)
        return True
    return False


def delete_transaction(tx_id) -> bool:
    """Deletes exactly one row. True only if a row was really deleted."""
    if _use_sb():
        r = _sb(
            "DELETE", "transactions",
            params={"id": f"eq.{tx_id}"},
            prefer="return=representation",
        )
        return len(_rows(r)) > 0
    global _mem_transactions
    before = len(_mem_transactions)
    _mem_transactions = [r for r in _mem_transactions if r["id"] != tx_id]
    return len(_mem_transactions) < before


def _group_rows(anchor_row):
    """The anchor plus every row that belongs to the same logical action (a split's personal
    expense and all its per-person IOU transfers share one linked_tx_id)."""
    link_id = anchor_row.get("linked_tx_id") or anchor_row["id"]
    if _use_sb():
        head = _rows(_sb("GET", "transactions", params={"select": "*", "id": f"eq.{link_id}"}))
        kids = _rows(_sb("GET", "transactions",
                         params={"select": "*", "linked_tx_id": f"eq.{link_id}", "order": "id.asc"}))
        found = head + kids
    else:
        found = [r for r in _mem_transactions if r["id"] == link_id or r.get("linked_tx_id") == link_id]
    seen, group = set(), []
    for r in found:
        if r["id"] not in seen:
            seen.add(r["id"])
            group.append(r)
    if anchor_row["id"] not in seen:
        group.append(anchor_row)
    return group


def delete_pool_contributions_linked(tx_ids) -> None:
    """Pool contributions made by 'put N into X pool' carry the id of their transfer row."""
    ids = [int(i) for i in tx_ids]
    if not ids:
        return
    if _use_sb():
        _sb("DELETE", "pool_contributions",
            params={"linked_tx_id": "in.(" + ",".join(str(i) for i in ids) + ")"})
        return
    global _mem_pool_contributions
    _mem_pool_contributions = [c for c in _mem_pool_contributions if c.get("linked_tx_id") not in ids]


def delete_transaction_group(anchor_row):
    """Delete a transaction and everything linked to it. Returns the rows actually deleted."""
    group = _group_rows(anchor_row)
    deleted = [r for r in group if delete_transaction(r["id"])]
    delete_pool_contributions_linked([r["id"] for r in group])
    return deleted


def delete_transaction_group_by_id(tx_id):
    tx = get_transaction(tx_id)
    if not tx:
        return []
    return delete_transaction_group(tx)


def delete_last_transaction():
    rows = _all_transactions(limit=1)
    if not rows:
        return None
    return delete_transaction_group(rows[0])


def delete_pool_contribution(row_id) -> bool:
    if _use_sb():
        r = _sb("DELETE", "pool_contributions", params={"id": f"eq.{row_id}"}, prefer="return=representation")
        return len(_rows(r)) > 0
    global _mem_pool_contributions
    before = len(_mem_pool_contributions)
    _mem_pool_contributions = [c for c in _mem_pool_contributions if c.get("id") != row_id]
    return len(_mem_pool_contributions) < before


def delete_pool_expense(row_id) -> bool:
    if _use_sb():
        r = _sb("DELETE", "pool_expenses", params={"id": f"eq.{row_id}"}, prefer="return=representation")
        return len(_rows(r)) > 0
    global _mem_pool_expenses
    before = len(_mem_pool_expenses)
    _mem_pool_expenses = [e for e in _mem_pool_expenses if e.get("id") != row_id]
    return len(_mem_pool_expenses) < before


BALANCE_BUCKETS = PAYMENT_METHODS


def _settings_key(method: str) -> str:
    return f"starting_balance:{method}"


def get_starting_balances() -> dict:
    result = {b: Decimal("0.00") for b in BALANCE_BUCKETS}
    if _use_sb():
        rows = _rows(_sb("GET", "settings",
                         params={"select": "key,value", "key": "like.starting_balance:*"}))
        for row in rows:
            method = row["key"].split(":", 1)[1]
            if method in result:
                try:
                    result[method] = Decimal(str(row["value"]))
                except InvalidOperation:
                    result[method] = Decimal("0.00")
        return result
    result.update(_mem_starting_balances)
    return result


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
    if _use_sb():
        _sb("POST", "settings",
            params={"on_conflict": "key"},
            json_body={"key": _settings_key(method), "value": str(start_bal)},
            prefer="resolution=merge-duplicates,return=minimal")
        return
    _mem_starting_balances[method] = start_bal


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


_ALIAS_TYPES = ("expense", "income")


def get_all_aliases() -> dict:
    """Confirmed aliases only (the fast path that lets us skip the LLM)."""
    if _use_sb():
        rows = _rows(_sb("GET", "aliases", params={"select": "*", "confirmed": "eq.true"}))
        return {
            row["note_key"]: {
                "type": row["type"],
                "category": row["category"],
                "payment_method": row.get("payment_method"),
                "confirmed": True,
            }
            for row in rows
        }
    return {k: v for k, v in _mem_aliases.items() if v.get("confirmed")}


def _get_alias_row(note_key):
    if _use_sb():
        rows = _rows(_sb("GET", "aliases", params={"select": "*", "note_key": f"eq.{note_key}"}))
        return rows[0] if rows else None
    return _mem_aliases.get(note_key)


def _put_alias(note_key, tx_type, category, payment_method, confirmed, count):
    if _use_sb():
        _sb("POST", "aliases",
            params={"on_conflict": "note_key"},
            json_body={
                "note_key": note_key, "type": tx_type, "category": category,
                "payment_method": payment_method, "confirmed": confirmed, "candidate_count": count,
            },
            prefer="resolution=merge-duplicates,return=minimal")
        return
    _mem_aliases[note_key] = {
        "type": tx_type, "category": category, "payment_method": payment_method,
        "confirmed": confirmed, "candidate_count": count,
    }


def _alias_ok(note_key, tx_type) -> bool:
    return bool(note_key) and len(note_key) <= 80 and tx_type in _ALIAS_TYPES


def learn_alias_manual(note_key, tx_type, category, payment_method=None):
    """Explicit human correction: trusted immediately."""
    if not _alias_ok(note_key, tx_type) or not category:
        return
    _put_alias(note_key, tx_type, category, payment_method, True, 1)


def learn_alias_auto(note_key, tx_type, category, payment_method=None):
    """Auto-learned from an LLM guess: needs a SECOND parse that agrees on type + category
    before it is trusted (confirmed) and allowed to skip the LLM."""
    if not _alias_ok(note_key, tx_type) or not category:
        return
    existing = _get_alias_row(note_key)
    if existing is None:
        _put_alias(note_key, tx_type, category, payment_method, False, 1)
        return
    if existing.get("confirmed"):
        return
    if existing.get("type") == tx_type and existing.get("category") == category:
        count = int(existing.get("candidate_count") or 1) + 1
        _put_alias(note_key, tx_type, category,
                   payment_method or existing.get("payment_method"), True, count)
    else:
        _put_alias(note_key, tx_type, category, payment_method, False, 1)


def _soft_aliases() -> dict:
    """Alias lookup is only an optimisation. If it fails we just call the LLM instead."""
    try:
        return get_all_aliases()
    except StorageError as e:
        print(f"[aliases] read failed, continuing without aliases: {e}")
        return {}


def _soft_learn_auto(note_key, tx_type, category, payment_method):
    """The transaction is already saved by the time this runs, so never fail the request over it."""
    try:
        learn_alias_auto(note_key, tx_type, category, payment_method)
    except StorageError as e:
        print(f"[aliases] auto-learn failed (non-fatal): {e}")


def _soft_learn_manual(note_key, tx_type, category, payment_method):
    try:
        learn_alias_manual(note_key, tx_type, category, payment_method)
    except StorageError as e:
        print(f"[aliases] manual-learn failed (non-fatal): {e}")


# --------------------------------------------------------- pools


def add_pool_contribution(pool_name, person, amount, note=None, linked_tx_id=None) -> int:
    global _mem_pool_id_counter
    amount_dec = _to_decimal(amount)
    if _use_sb():
        r = _sb("POST", "pool_contributions",
                json_body={"pool": pool_name, "person": person, "amount": str(amount_dec),
                           "note": note, "linked_tx_id": linked_tx_id},
                prefer="return=representation")
        row_id = _first_id(r, "pool_contributions")
        return row_id
    row_id = _mem_pool_id_counter
    _mem_pool_id_counter += 1
    _mem_pool_contributions.append({
        "id": row_id, "pool": pool_name, "person": person, "amount": amount_dec, "note": note,
        "linked_tx_id": linked_tx_id, "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return row_id


def add_pool_expense(pool_name, amount, category, note=None) -> int:
    global _mem_pool_id_counter
    amount_dec = _to_decimal(amount)
    if _use_sb():
        r = _sb("POST", "pool_expenses",
                json_body={"pool": pool_name, "amount": str(amount_dec), "category": category, "note": note},
                prefer="return=representation")
        row_id = _first_id(r, "pool_expenses")
        return row_id
    row_id = _mem_pool_id_counter
    _mem_pool_id_counter += 1
    _mem_pool_expenses.append({
        "id": row_id, "pool": pool_name, "amount": amount_dec, "category": category, "note": note,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return row_id


def get_pool_contributions(pool_name=None):
    if _use_sb():
        params = {"select": "*"}
        if pool_name:
            params["pool"] = f"eq.{pool_name}"
        return _rows(_sb("GET", "pool_contributions", params=params))
    rows = _mem_pool_contributions
    return [r for r in rows if r["pool"] == pool_name] if pool_name else list(rows)


def get_pool_expenses(pool_name=None):
    if _use_sb():
        params = {"select": "*"}
        if pool_name:
            params["pool"] = f"eq.{pool_name}"
        return _rows(_sb("GET", "pool_expenses", params=params))
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


def reset_all_data() -> None:
    """Really wipes the ledger (Supabase tables too when configured)."""
    global _mem_id_counter, _mem_pool_id_counter
    if _use_sb():
        _sb("DELETE", "transactions", params={"id": "gt.0"})
        _sb("DELETE", "pool_contributions", params={"id": "gt.0"})
        _sb("DELETE", "pool_expenses", params={"id": "gt.0"})
        _sb("DELETE", "aliases", params={"note_key": "neq.__none__"})
        _sb("DELETE", "settings", params={"key": "like.starting_balance:*"})
        _sb("DELETE", "chat_log", params={"id": "gt.0"})
        return
    _mem_transactions.clear()
    _mem_pool_contributions.clear()
    _mem_pool_expenses.clear()
    _mem_aliases.clear()
    for b in PAYMENT_METHODS:
        _mem_starting_balances[b] = Decimal("0.00")
    _mem_id_counter = 1
    _mem_pool_id_counter = 1


# ------------------------------------------------------------------ nlp layer

def _validate_item(data):
    if not data or data.get("type") not in ("expense", "income"):
        return None
    try:
        amount = _to_decimal(data["amount"])
    except (TypeError, ValueError, KeyError, InvalidOperation):
        return None
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
    valid_categories = EXPENSE_CATEGORIES if data["type"] == "expense" else INCOME_CATEGORIES
    category = data.get("category") if data.get("category") in valid_categories else "other"
    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else None
    note = str(data["note"]).strip()[:100] if data.get("note") else ""
    return {"type": data["type"], "amount": amount, "category": category, "payment_method": payment_method, "note": note or None}


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
    if amount <= 0 or amount > MAX_AMOUNT:
        return None

    people_raw = data.get("people")
    if isinstance(people_raw, list) and people_raw:
        people = [_clean_name(p) for p in people_raw]
    elif data.get("person"):
        people = [_clean_name(data["person"])]
    else:
        people = []
    # drop empties, "me"/"myself", and duplicates (keeps order)
    people = [p for p in dict.fromkeys(people) if p and p not in _SELF_WORDS]

    pool = _clean_name(data.get("pool"), hyphen=True) if data.get("pool") else None
    pool = pool or None

    if share_type in ("split", "lend", "borrow", "repayment") and not people:
        return None
    if share_type == "pool_contribution_other" and (not pool or not people):
        return None
    if share_type in ("contribution", "pool_expense") and not pool:
        return None

    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else "cash"
    category = data.get("category") if data.get("category") in EXPENSE_CATEGORIES else "other"
    note = str(data["note"]).strip()[:100] if data.get("note") else None
    repayment_direction = data.get("repayment_direction") if data.get("repayment_direction") in ("they_paid_me", "i_paid_them") else "they_paid_me"

    return {
        "share_type": share_type, "amount": amount, "people": people, "pool": pool,
        "payment_method": payment_method, "category": category, "note": note or None,
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
        tx_id = add_transfer(amount, method, f"pool:{pool}", note, pool=pool)
        add_pool_contribution(pool, "me", amount, note, linked_tx_id=tx_id)
        return {"kind": "contribution", "amount": amount, "who": pool, "method": method}

    elif share_type == "pool_contribution_other":
        add_pool_contribution(pool, person, amount, note)
        return {"kind": "pool_contribution_other", "amount": amount, "who": pool, "person": person}

    elif share_type == "pool_expense":
        add_pool_expense(pool, amount, category, note)
        return {"kind": "pool_expense", "amount": amount, "who": pool, "category": category}

    return None


_DEBT_HINT_RE = re.compile(
    r"\b(?:split|share|shares|gave|give|lent|lend|loan|borrow\w*|back|return\w*|repa\w*|settle\w*|owe\w*|"
    r"from|pool|fund|contribut\w*|put|sent|send|transfer\w*|ways|paid\s+(?:for|me|to))\b", re.IGNORECASE)


def _demote_unbacked_debt(items, text):
    """The LLM sometimes reads a bare name ('rohan rahul 800 upi') as a debt. If the message holds no
    debt/shared wording at all, turn such items back into a plain expense whose note is the name."""
    if _DEBT_HINT_RE.search(text or ""):
        return items
    out = []
    for it in items:
        if it.get("share_type"):
            note = it.get("note") or " ".join(it.get("people") or []) or it.get("pool")
            plain = _validate_item({"type": "expense", "amount": it["amount"], "category": it.get("category"),
                                    "payment_method": it.get("payment_method"), "note": note})
            if plain:
                out.append(plain)
                continue
        out.append(it)
    return out


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
                return _demote_unbacked_debt(results, text)
    except Exception as e:
        print(f"Groq API call error: {e}")
    return _fallback_rule_parse(text)


_INCOME_RE = re.compile(
    r"\b(?:income|salary|got paid|got|received|earned|payout|refund|credited|deposited)\b", re.IGNORECASE
)

_CATEGORY_WORDS = (
    ("food", ["lunch", "dinner", "breakfast", "food", "cafe", "restaurant", "burger", "pizza", "juice",
              "biscuit", "tea", "coffee", "snack"]),
    ("groceries", ["milk", "groceries", "grocery", "veggies", "vegetables", "fruit", "market"]),
    ("transport", ["uber", "cab", "bus", "train", "metro", "flight", "transit", "petrol", "fuel"]),
    ("rent", ["rent"]),
    ("bills", ["electricity", "wifi", "bill", "water"]),
    ("subscriptions", ["netflix", "spotify", "prime", "hotstar", "youtube", "subscription"]),
    ("entertainment", ["movie", "cinema", "concert", "game", "gaming", "show"]),
    ("shopping", ["amazon", "flipkart", "myntra", "shirt", "shoes", "clothes", "mall"]),
    ("health", ["medicine", "doctor", "pharmacy", "hospital", "medical", "clinic"]),
    ("education", ["course", "tuition", "exam", "notebook", "book", "stationery"]),
    ("fitness", ["gym", "protein", "yoga"]),
    ("travel", ["hotel", "hostel", "resort", "trip", "tour", "ticket", "entry", "visit", "monument", "fort", "stay"]),
)


def _has_word(text: str, words) -> bool:
    """Whole-word match (optional plural 's'), so 'tea' does not match 'steak'/'team',
    'bus' does not match 'business', 'got' does not match 'forgot'/'gotta'."""
    pattern = r"\b(?:" + "|".join(re.escape(w) for w in words) + r")s?\b"
    return re.search(pattern, text or "") is not None


def _guess_category(note_str: str, tx_type="expense") -> str:
    s = (note_str or "").lower()
    if tx_type == "income":
        if _has_word(s, ["freelance"]):
            return "freelance"
        if _has_word(s, ["refund"]):
            return "refund"
        if _has_word(s, ["gift"]):
            return "gift"
        return "salary"
    for category, words in _CATEGORY_WORDS:
        if _has_word(s, words):
            return category
    return "other"


def _parse_single_fallback_item(text):
    tokens = text.lower().replace("₹", " ").replace("$", " ").split()
    if not tokens:
        return None

    tx_type = "income" if _INCOME_RE.search(text) else "expense"

    amount = None
    payment_method = None
    category = None
    note_words = []

    for tok in tokens:
        clean = tok.strip(".,;")
        if amount is None:
            val = _amount_from_str(clean)
            if val is not None:
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
        if not any(ch.isalnum() for ch in clean):
            continue  # stray "-", "=", ":" etc. never belong in a note
        note_words.append(clean)

    if amount is None:
        return None
    amount = _to_decimal(amount)  # round FIRST: 0.001 becomes 0.00 and must be rejected
    if amount <= 0 or amount > MAX_AMOUNT:
        return None

    while note_words and note_words[0] in _FILLER_WORDS:
        note_words.pop(0)
    while note_words and note_words[-1] in _FILLER_WORDS:
        note_words.pop()

    if not category:
        category = _guess_category(" ".join(note_words), tx_type)

    note = " ".join(note_words)[:100].strip() or None
    return {"type": tx_type, "amount": amount, "category": category,
            "payment_method": payment_method, "note": note}


def _debt_reply(parsed, r):
    """(reply_text, html) for one applied debt/shared item. All user-derived text is escaped."""
    kind = r.get("kind") if r else None
    cap = _nm

    if kind == "split":
        people_str = ", ".join(cap(p) for p, _ in r["people"])
        plain_people = ", ".join(_pn(p) for p, _ in r["people"])
        reply_text = f"Split ₹{fmt(parsed['amount'])} ({parsed['category']}) with {plain_people}. Your share: ₹{fmt(r['my_share'])}."
        breakdown_lines = "".join(
            f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{cap(p)} owes you</span>"
            f"<span class='font-numeral text-[10px] font-bold text-[#059669]'>+₹{fmt(a)}</span></div>"
            for p, a in r["people"]
        )
        html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1 flex-wrap">
                <span class="bg-[#8B5CF6] text-white px-1.5 py-0.5 font-bold border border-[#6D28D9]" style="font-size:10px;">SPLIT BILL</span>
                <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">+20 EXP</span>
              </div>
              <p class="text-[12px]">Split <strong>₹{fmt(parsed['amount'])}</strong> for <strong>{cap(parsed['category'])}</strong> with <strong>{people_str}</strong>.</p>
              <div class="pixel-window-jrpg p-2 text-[11px] space-y-0.5">
                <div class="flex justify-between py-0.5 border-b border-[#E5DFC9]"><span>Your share (Expense)</span><span class="font-numeral font-bold text-[#E15554]">-₹{fmt(r['my_share'])}</span></div>
                {breakdown_lines}
              </div>
            </div>
            """
    elif kind == "lend":
        reply_text = f"Lent ₹{fmt(r['amount'])} to {_pn(r['who'])}."
        html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#3B82F6] text-white px-1.5 py-0.5 font-bold border border-[#1D4ED8]" style="font-size:10px;">IOU CREATED</span>
              </div>
              <p class="text-[12px]">Lent <strong>₹{fmt(r['amount'])}</strong> to <strong>{cap(r['who'])}</strong> via {_esc(r['method'].upper())}.</p>
            </div>
            """
    elif kind == "borrow":
        reply_text = f"Borrowed ₹{fmt(r['amount'])} from {_pn(r['who'])}."
        html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#E15554] text-white px-1.5 py-0.5 font-bold border border-[#991B1B]" style="font-size:10px;">DEBT INCURRED</span>
              </div>
              <p class="text-[12px]">Borrowed <strong>₹{fmt(r['amount'])}</strong> from <strong>{cap(r['who'])}</strong> into {_esc(r['method'].upper())}.</p>
            </div>
            """
    elif kind == "repayment":
        if r.get("direction") == "they_paid_me":
            reply_text = f"{_pn(r['who'])} paid back ₹{fmt(r['amount'])}."
            html_res = f"""
                <div class="space-y-1.5">
                  <div class="flex items-center gap-1.5 mb-1">
                    <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">DEBT SETTLED</span>
                  </div>
                  <p class="text-[12px]"><strong>{cap(r['who'])}</strong> paid back <strong>₹{fmt(r['amount'])}</strong> via {_esc(r['method'].upper())}.</p>
                </div>
                """
        else:
            reply_text = f"Paid back ₹{fmt(r['amount'])} to {_pn(r['who'])}."
            html_res = f"""
                <div class="space-y-1.5">
                  <div class="flex items-center gap-1.5 mb-1">
                    <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">DEBT CLEARED</span>
                  </div>
                  <p class="text-[12px]">Paid back <strong>₹{fmt(r['amount'])}</strong> to <strong>{cap(r['who'])}</strong> via {_esc(r['method'].upper())}.</p>
                </div>
                """
    elif kind == "contribution":
        reply_text = f"Put ₹{fmt(r['amount'])} into {str(r['who']).upper()} pool."
        html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#F59E0B] text-white px-1.5 py-0.5 font-bold border border-[#B45309]" style="font-size:10px;">POOL DEPOSIT</span>
              </div>
              <p class="text-[12px]">Deposited <strong>₹{fmt(r['amount'])}</strong> into <strong>{_esc(str(r['who']).upper())}</strong> pool via {_esc(r['method'].upper())}.</p>
            </div>
            """
    elif kind == "pool_contribution_other":
        reply_text = f"{_pn(r['person'])} put ₹{fmt(r['amount'])} into {str(r['who']).upper()} pool."
        html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#F59E0B] text-white px-1.5 py-0.5 font-bold border border-[#B45309]" style="font-size:10px;">POOL DEPOSIT</span>
              </div>
              <p class="text-[12px]"><strong>{cap(r['person'])}</strong> deposited <strong>₹{fmt(r['amount'])}</strong> into <strong>{_esc(str(r['who']).upper())}</strong> pool.</p>
            </div>
            """
    elif kind == "pool_expense":
        reply_text = f"Spent ₹{fmt(r['amount'])} from {str(r['who']).upper()} pool."
        html_res = f"""
            <div class="space-y-1.5">
              <div class="flex items-center gap-1.5 mb-1">
                <span class="bg-[#E15554] text-white px-1.5 py-0.5 font-bold border border-[#991B1B]" style="font-size:10px;">POOL EXPENSE</span>
              </div>
              <p class="text-[12px]">Spent <strong>₹{fmt(r['amount'])}</strong> ({cap(r['category'])}) from <strong>{_esc(str(r['who']).upper())}</strong> pool.</p>
            </div>
            """
    else:
        reply_text = "Debt item processed."
        html_res = "<p>Debt transaction recorded.</p>"
    return reply_text, html_res


def _fallback_debt_parse(text):
    """Best-effort regex recognizer for the most common shared/debt phrasings, used ONLY when the
    Groq API is unavailable (no key, or the call failed). Narrower than the LLM path: one event per
    message. Names may have several words ('rohan rahul') and stop at a payment method or amount.
    Treats the WHOLE message as one event rather than chunking it, since chunking on commas would
    cut the amount away from phrases like '600 dinner upi, split with rahul'."""
    tl = " ".join(text.strip().lower().split())
    N = r"(\d+(?:\.\d+)?k?)"

    def _method_in(s):
        for m in PAYMENT_METHODS:
            if re.search(rf"\b{m}\b", s):
                return m
        return None

    def _pool_name(s):
        return _clean_name(re.sub(r"^(?:my|our|the)\s+", "", s.strip()), hyphen=True) or None

    m = re.search(N + r"\b.*?\b(?:split|share)\b(?:\s+\w+){0,3}?\s+with\s+(.+)$", tl)
    if m:
        amount, people = _amount_from_str(m.group(1)), _take_people(m.group(2))
        if amount and people:
            pre = re.split(r"\b(?:split|share)\b", tl)[0]
            return _validate_debt_item({
                "share_type": "split", "amount": amount, "people": people, "category": _guess_category(pre),
                "payment_method": _method_in(tl), "note": _leftover_note(pre),
            })

    m = re.search(r"\b(?:gave|lent|lend)\s+(?!\d)(.+?)\s+" + N + r"\b", tl)
    if m:
        amount, name = _amount_from_str(m.group(2)), _take_name(m.group(1))
        if amount and name:
            return _validate_debt_item({"share_type": "lend", "amount": amount, "people": [name],
                                        "payment_method": _method_in(tl), "note": None})

    m = re.search(r"\b(?:gave|lent|lend)\s+" + N + r"\b(?:\s+(?:upi|cash|card|netbanking))?\s+to\s+(.+)$", tl)
    if m:
        amount, name = _amount_from_str(m.group(1)), _take_name(m.group(2))
        if amount and name:
            return _validate_debt_item({"share_type": "lend", "amount": amount, "people": [name],
                                        "payment_method": _method_in(tl), "note": None})

    m = re.search(r"\bborrowed\s+" + N + r"\b.*?\bfrom\s+(.+)$", tl)
    if m:
        amount, name = _amount_from_str(m.group(1)), _take_name(m.group(2))
        if amount and name:
            return _validate_debt_item({"share_type": "borrow", "amount": amount, "people": [name],
                                        "payment_method": _method_in(tl), "note": None})

    m = re.search(r"\bpaid\s+(?!back\b|me\b)(.+?)\s+back\s+" + N + r"\b", tl)
    if m:
        amount, name = _amount_from_str(m.group(2)), _take_name(m.group(1))
        if amount and name:
            return _validate_debt_item({"share_type": "repayment", "repayment_direction": "i_paid_them",
                                        "amount": amount, "people": [name], "payment_method": _method_in(tl), "note": None})

    m = re.search(r"\bpaid\s+back\s+" + N + r"\b(?:\s+(?:upi|cash|card|netbanking))?\s+to\s+(.+)$", tl)
    if m:
        amount, name = _amount_from_str(m.group(1)), _take_name(m.group(2))
        if amount and name:
            return _validate_debt_item({"share_type": "repayment", "repayment_direction": "i_paid_them",
                                        "amount": amount, "people": [name], "payment_method": _method_in(tl), "note": None})

    m = re.search(r"^(.+?)\s+(?:paid(?:\s+me)?\s+back|paid\s+me|returned(?:\s+me)?|repaid(?:\s+me)?)\s+" + N + r"\b", tl)
    if m:
        amount, name = _amount_from_str(m.group(2)), _take_name_tail(m.group(1))
        if amount and name:
            return _validate_debt_item({"share_type": "repayment", "repayment_direction": "they_paid_me",
                                        "amount": amount, "people": [name], "payment_method": _method_in(tl), "note": None})

    if not re.search(r"\b(?:pool|fund)\b", tl):
        m = re.search(r"\b(?:got|received)\s+" + N + r"\b.*?\bfrom\s+(.+)$", tl)
        if m:
            amount, name = _amount_from_str(m.group(1)), _take_name(m.group(2))
            if amount and name:
                return _validate_debt_item({"share_type": "repayment", "repayment_direction": "they_paid_me",
                                            "amount": amount, "people": [name], "payment_method": _method_in(tl), "note": None})

    m = re.search(r"^(.+?)\s+put\s+" + N + r"\b.*?\b(?:in|into|to)\s+(?:the\s+)?(.+?)\s+(?:pool|fund)\b", tl)
    if m:
        amount, who, pool = _amount_from_str(m.group(2)), _take_name_tail(m.group(1)), _pool_name(m.group(3))
        if amount and who and pool:
            return _validate_debt_item({"share_type": "pool_contribution_other", "amount": amount,
                                        "people": [who], "pool": pool, "note": None})

    m = re.search(r"\b(?:put|added|add|contributed)\s+" + N + r"\b.*?\b(?:in|into|to)\s+(?:the\s+)?(.+?)\s+(?:pool|fund)\b", tl)
    if m:
        amount, pool = _amount_from_str(m.group(1)), _pool_name(m.group(2))
        if amount and pool:
            return _validate_debt_item({"share_type": "contribution", "amount": amount, "pool": pool,
                                        "payment_method": _method_in(tl), "note": None})

    m = re.search(N + r"\b.*?\bfrom\s+(?:the\s+)?(.+?)\s+(?:pool|fund)\b", tl)
    if m:
        amount, pool = _amount_from_str(m.group(1)), _pool_name(m.group(2))
        if amount and pool:
            return _validate_debt_item({"share_type": "pool_expense", "amount": amount, "pool": pool,
                                        "category": "other", "note": None})

    return None


def _split_chunks(text):
    """Split on newline / comma / semicolon / 'and', but never leave a piece without a number:
    an amount-less piece ('rohan rahul') is glued onto its neighbour so no words are silently dropped
    ('rohan rahul and taj mahal for 800 upi' stays ONE item)."""
    raw = re.split(r"([\n,;]|\band\b)", text, flags=re.IGNORECASE)
    pairs = [("", raw[0])] + [(raw[i], raw[i + 1]) for i in range(1, len(raw) - 1, 2)]
    merged, carry, carry_glue = [], "", " "
    for sep, chunk in pairs:
        chunk = chunk.strip()
        if not chunk:
            continue
        glue = " and " if sep.strip().lower() == "and" else " "
        had_carry = bool(carry)
        if carry:
            chunk, carry = carry + glue + chunk, ""
        if re.search(r"\d", chunk):
            merged.append(chunk)
        else:
            carry = chunk
            if not had_carry:
                carry_glue = glue
    if carry and merged:
        merged[-1] += carry_glue + carry
    return merged


def _fallback_rule_parse(text):
    """Rule-based regex parser when Groq API key is absent or unavailable."""
    debt_item = _fallback_debt_parse(text)
    if debt_item:
        return [debt_item]

    chunks = _split_chunks(text)

    if len(chunks) == 1:
        subs = list(re.finditer(r"\d+(?:\.\d+)?k?\s+.*?(?=\s+\d+(?:\.\d+)?k?\s+|$)", chunks[0], flags=re.IGNORECASE))
        if len(subs) > 1:
            pieces = [s.group(0).strip() for s in subs]
            pieces[0] = (chunks[0][:subs[0].start()] + pieces[0]).strip()  # words typed before the first amount
            chunks = pieces

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

    if amount is None or not note_tokens:
        return None

    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
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


_AUDIO_MIME = {
    "ogg": "audio/ogg", "oga": "audio/ogg", "webm": "audio/webm", "m4a": "audio/mp4", "mp4": "audio/mp4",
    "mp3": "audio/mpeg", "mpeg": "audio/mpeg", "mpga": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
}


def transcribe_voice(audio_bytes, filename="voice.ogg"):
    """Returns (text, error). Exactly one of them is None. Never invents a transcript."""
    if not GROQ_API_KEY:
        return None, "Voice input is not configured (GROQ_API_KEY missing)."
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in _AUDIO_MIME:
        ext = "ogg"
    filename = f"voice.{ext}"
    try:
        r = requests.post(
            GROQ_AUDIO_API,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            files={"file": (filename, audio_bytes, _AUDIO_MIME[ext])},
            data={"model": GROQ_WHISPER_MODEL, "response_format": "text"},
            timeout=30,
        )
    except requests.RequestException as e:
        print(f"Whisper STT network error: {e}")
        return None, "Could not reach the transcription service. Nothing was logged."
    if r.status_code >= 300:
        print(f"Whisper STT error: {r.status_code} {r.text[:300]}")
        return None, f"Transcription failed (HTTP {r.status_code}). Nothing was logged."
    text = r.text.strip()
    if not text:
        return None, "I couldn't hear anything in that recording. Nothing was logged."
    return text, None


# --------------------------------------------------------------------- chat log helpers


def log_chat_message(role: str, content: str) -> None:
    """Insert one chat turn into chat_log. Non-fatal: logs on error rather than crashing."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    if role == "assistant" and not content.lstrip().startswith("<"):
        content = _esc(content)  # plain-text replies echo user input; the page re-renders history as HTML
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
        note = f" ({_esc(r['note'])})" if r.get("note") else ""
        pay = f" [{_esc(r['payment_method'])}]" if r.get("payment_method") else ""
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
        label = f"{_nm(person)} owes you" if amt > 0 else f"You owe {_nm(person)}"
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


def _unwrap(res):
    """_process_text returns either a dict or a (dict, http_status) tuple (usage errors)."""
    if isinstance(res, tuple):
        return res[0], res[1]
    return res, 200


# ---------------------------------------------------------------------- Demo login (cookie session)

_OPEN_API_PATHS = {"/api/login", "/api/logout", "/api/session"}


def _is_logged_in() -> bool:
    return session.get("user") == DEMO_USERNAME


@app.before_request
def _require_login():
    """Every /api/* route except login/logout/session needs a valid signed session cookie."""
    path = request.path
    if request.method == "OPTIONS" or not path.startswith("/api/") or path in _OPEN_API_PATHS:
        return None
    if _is_logged_in():
        return None
    return jsonify({"ok": False, "auth": False, "text": "Please sign in first."}), 401


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    # case-insensitive username (phones auto-capitalise), exact password, constant-time compares
    user_ok = hmac.compare_digest(username.lower().encode("utf-8"), DEMO_USERNAME.lower().encode("utf-8"))
    pass_ok = hmac.compare_digest(password.encode("utf-8"), DEMO_PASSWORD.encode("utf-8"))
    if user_ok and pass_ok:
        session.clear()
        session["user"] = DEMO_USERNAME
        session.permanent = True
        return _no_store(jsonify({"ok": True, "username": DEMO_USERNAME}))
    time.sleep(0.4)  # blunt the guessing speed a little
    return _no_store(jsonify({"ok": False, "text": "Wrong username or password."})), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return _no_store(jsonify({"ok": True}))


@app.route("/api/session", methods=["GET"])
def api_session():
    authed = _is_logged_in()
    return _no_store(jsonify({"ok": True, "authenticated": authed, "username": DEMO_USERNAME if authed else None}))


@app.errorhandler(StorageError)
def _handle_storage_error(e):
    """Supabase is configured but failed: say so loudly instead of pretending everything is fine."""
    print(f"[storage] {e}")
    return jsonify({
        "ok": False,
        "text": f"Database error ({e}). Check /history before retrying — the last action may not have been saved.",
    }), 503


@app.errorhandler(Exception)
def _handle_unexpected(e):
    if isinstance(e, HTTPException):
        return e
    traceback.print_exc()
    return jsonify({"ok": False, "text": "Internal error. Check /history before retrying."}), 500


@app.route("/", methods=["GET"])
def index():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return send_file(index_path)
    root_index = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(root_index):
        return send_file(root_index)
    return jsonify({"ok": True, "service": "Fogless 16-bit JRPG Finance Companion"})


@app.route("/login", methods=["GET"])
def login_page():
    return send_file(os.path.join(FRONTEND_DIR, "login.html"))


@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    if not filename.startswith("api/"):
        file_path = safe_join(FRONTEND_DIR, filename)  # refuses ../ escapes
        if file_path and os.path.isfile(file_path):
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
                "category": r.get("category") or ("transfer" if r.get("type") == "transfer" else "other"),
                "note": r.get("note"),
                "payment_method": r.get("payment_method"),
                "created_at": r.get("created_at"),
                "created_at_display": _fmt_dt(r.get("created_at")),
            }
            for r in history
        ],
    })


def _process_text(text: str, allow_commands: bool = True):
    """Entry point for web, voice and Telegram. Cleans the amounts first (₹ / rs / 1,200 / minus signs),
    runs the real handler, then appends the 'negative value converted to positive' notice to the reply."""
    text = (text or "").strip()
    if len(text) > MAX_TEXT_LEN:
        return ({"ok": False, "text": f"Message too long (max {MAX_TEXT_LEN} characters)."}, 400)
    _tl.notices = []
    if allow_commands and text.startswith("/"):
        clean, _tl.negs = text, []
    else:
        clean, _tl.negs = _normalize_amount_text(text)
    payload, status = _unwrap(_process_text_core(clean, allow_commands))
    payload = _with_notice(dict(payload), _tl.notices)
    return payload if status == 200 else (payload, status)


def _process_text_core(text: str, allow_commands: bool = True):
    """Processes natural language input or slash commands and returns a response dictionary
    (or a (dict, http_status) tuple for usage errors). allow_commands=False is used for voice."""
    if allow_commands and text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]  # Telegram groups send /stats@YourBot
        if cmd not in _KNOWN_COMMANDS:
            reply_text = ("Unknown command. Try /stats, /balance, /history, /debts, /pool, /settle, /undo, "
                          "/delete, /aliases - or just type what you spent, e.g. 24 coffee upi")
            log_chat_message("assistant", reply_text)
            return {"ok": True, "text": reply_text, "balance": float(get_balance()), "today_spent": float(get_today_expense())}
        args = parts[1:]

        if cmd == "/stats":
            period = args[0].lower() if args else "month"
            if period not in ("today", "week", "month", "all"):
                return ({"ok": False, "text": "Usage: /stats [today|week|month|all]"}, 400)
            html = format_stats_html(period)
            reply_text = f"Stats for {period} period."
            log_chat_message("assistant", html)
            return {"ok": True, "html": html, "text": reply_text, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

        elif cmd in ("/balance", "/setbalance"):
            if args:
                target_method = "cash"
                target_amount = None
                target_neg = False
                for a in args:
                    clean_a = a.lower().replace("₹", "").replace("$", "").replace(",", "")
                    if clean_a in PAYMENT_METHODS:
                        target_method = clean_a
                    else:
                        val, was_neg = _parse_amount_token(clean_a)
                        if val is not None:
                            target_amount, target_neg = val, was_neg
                if target_amount is not None and target_amount > MAX_AMOUNT:
                    return ({"ok": False, "text": "That balance is too large to be real."}, 400)
                if target_amount is not None:
                    if target_neg:
                        _tl.notices.append(f"Negative balance -₹{fmt(target_amount)} converted to positive ₹{fmt(target_amount)}.")
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
            # order-free: /settle rohan rahul 500 upi   |   /settle 500 upi rohan rahul
            amt, was_neg, method, name_tokens = None, False, "cash", []
            for a in args:
                low = a.lower()
                val, neg = _parse_amount_token(low)
                if val is not None and amt is None:
                    amt, was_neg = _to_decimal(val), neg
                elif low in PAYMENT_METHODS:
                    method = low
                else:
                    name_tokens.append(low)
            person = _clean_name(" ".join(name_tokens))
            if not person or person in _SELF_WORDS or amt is None or amt <= 0 or amt > MAX_AMOUNT:
                reply_text = "Usage: /settle <person> <amount> [payment_method]"
                log_chat_message("assistant", reply_text)
                return ({"ok": False, "text": reply_text}, 400)
            if was_neg:
                _tl.notices.append(f"Negative amount -₹{fmt(amt)} converted to positive ₹{fmt(amt)}.")
            item = _validate_debt_item({
                "share_type": "repayment",
                "repayment_direction": "they_paid_me",
                "amount": amt,
                "people": [person],
                "payment_method": method,
            })
            if item:
                r = _apply_debt_transaction(item)
                reply_text = f"{_pn(person)} paid back ₹{fmt(amt)}."
                html_res = f"""
                <div class="space-y-1.5">
                  <div class="flex items-center gap-1.5 mb-1">
                    <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">DEBT SETTLED</span>
                  </div>
                  <p class="text-[12px]"><strong>{_nm(person)}</strong> paid back <strong>₹{fmt(amt)}</strong> via {_esc(method.upper())}.</p>
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
            limit = max(1, min(int(args[0]), 50)) if args and re.fullmatch(r"[0-9]{1,6}", args[0]) else 5
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
            if not args or not re.fullmatch(r"[0-9]{1,15}", args[0]):
                return ({"ok": False, "text": "Usage: /delete <id>"}, 400)
            tx_id = int(args[0])
            group = delete_transaction_group_by_id(tx_id)
            if group:
                extra = f" and {len(group) - 1} linked entr{'y' if len(group) == 2 else 'ies'}" if len(group) > 1 else ""
                reply_text = f"Deleted #{tx_id}{extra}."
            else:
                reply_text = f"No transaction #{tx_id} found."
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
            if not args or args[0].lower() != "confirm":
                reply_text = ("⚠ /reset permanently deletes ALL transactions, pools, aliases, starting balances "
                              "and chat history. If you really mean it, send: /reset confirm")
                return {"ok": True, "text": reply_text, "balance": float(get_balance()), "today_spent": float(get_today_expense())}
            if _use_sb() and not ALLOW_RESET:
                reply_text = ("Reset is disabled on the live database. Set ALLOW_RESET=1 in the environment, "
                              "redeploy, send /reset confirm, then remove it again.")
                return {"ok": True, "text": reply_text, "balance": float(get_balance()), "today_spent": float(get_today_expense())}
            reset_all_data()
            reply_text = "All data reset (database and chat history wiped)." if _use_sb() else "All local data reset."
            log_chat_message("assistant", reply_text)
            return {"ok": True, "text": reply_text, "balance": 0.0, "today_spent": 0.0}

        elif cmd == "/debts":
            html = format_debts_html()
            log_chat_message("assistant", html)
            return {"ok": True, "text": "IOU ledger shown.", "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())}

        elif cmd == "/pool":
            pool_name = (_clean_name(" ".join(args), hyphen=True) or None) if args else None
            if not pool_name:
                pools = list_known_pools()
                if not pools:
                    reply_text = "No pools created yet. Try: put 500 into goa pool"
                    html = """
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
                f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{_nm(p)}</span><span class='font-numeral text-[10px] font-bold'>+₹{fmt(a)}</span></div>"
                for p, a in s["by_person"].items()
            )
            expense_lines_list = []
            for e in s["expenses"]:
                note_str = f" ({_esc(e['note'])})" if e.get("note") else ""
                expense_lines_list.append(
                    f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{e.get('category','other').capitalize()}{note_str}</span><span class='font-numeral text-[10px] font-bold text-[#E15554]'>-₹{fmt(e['amount'])}</span></div>"
                )
            expense_lines = "".join(expense_lines_list)
            net_lines = "".join(
                f"<div class='flex justify-between py-0.5'><span class='{'text-[#059669]' if v >= 0 else 'text-[#E15554]'} font-bold'>{_nm(p)}</span><span class='font-numeral text-[10px]'>{'gets back ₹' + fmt(v) if v >= 0 else 'owes ₹' + fmt(abs(v))}</span></div>"
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
                    if _tl.negs:
                        _tl.notices.append(f"Negative balance -₹{fmt(val)} converted to positive ₹{fmt(val)}.")
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

    aliases = _soft_aliases()
    transactions = parse_multi(text, aliases)
    _tl.notices.extend(_flag_negatives(transactions or [], _tl.negs))
    if not transactions:
        reply_text = f"Could not parse amount from '{text}'. Hint: type coins first, e.g. 12 notebook cash"
        log_chat_message("assistant", reply_text)
        return {
            "ok": True,
            "text": reply_text,
            "html": f"<p>Could not parse amount from <span class='bg-[#F87171] text-white px-1 text-[13px] font-mono'>\"{_esc(text)}\"</span>. Hint: type the coins first, e.g. <strong class='underline decoration-2'>12 notebook cash</strong>.</p>",
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        }

    # A lone debt/shared item
    if len(transactions) == 1 and transactions[0].get("share_type"):
        parsed = transactions[0]
        r = _apply_debt_transaction(parsed)
        reply_text, html_res = _debt_reply(parsed, r)
        log_chat_message("assistant", html_res)
        return {
            "ok": True,
            "html": html_res,
            "text": reply_text,
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        }

    # One or more items (plain and/or debt) -- each is applied and reported on its own
    logged = []
    for parsed in transactions:
        if parsed.get("share_type"):
            r = _apply_debt_transaction(parsed)
            summary, _ = _debt_reply(parsed, r)
            logged.append({"debt": True, "summary": summary, "amount": parsed["amount"]})
        else:
            tx_id = add_transaction(parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
            note_key = (parsed.get("note") or "").lower().strip()
            _soft_learn_auto(note_key, parsed["type"], parsed["category"], parsed["payment_method"])
            logged.append({**parsed, "id": tx_id, "debt": False})

    if len(logged) == 1:
        last_tx = logged[0]
        sign = "+" if last_tx["type"] == "income" else "-"
        badge_bg = "bg-[#10B981]" if last_tx["type"] == "income" else "bg-[#E15554]"
        reply_text = f"Logged ₹{fmt(last_tx['amount'])} on {last_tx['category']}"
        pay_html = f" via {_esc(last_tx['payment_method'].upper())}" if last_tx.get("payment_method") else ""
        html_res = f"""
        <div class="flex items-center gap-1.5 mb-1 flex-wrap">
          <span class="{badge_bg} text-white px-1.5 py-0.5 font-bold border border-[#7F1D1D]" style="font-size:10px;">{sign}₹{fmt(last_tx['amount'])} GP</span>
          <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-bold border border-[#065F46]" style="font-size:10px;">+15 EXP</span>
          <span class="bg-[#3B82F6] text-white px-1.5 py-0.5 font-bold border border-[#1E40AF]" style="font-size:10px;">{_esc(last_tx['category'].upper())}</span>
        </div>
        <p>Logged <strong>₹{fmt(last_tx['amount'])}</strong> under <strong>{_esc(last_tx['category'].capitalize())}</strong>{pay_html}.</p>
        """
        log_chat_message("assistant", html_res)
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

    has_debt = any(x["debt"] for x in logged)
    mixed = len({x["type"] for x in logged if not x["debt"]}) > 1  # income + expense must not be added together
    plain_total = sum((x["amount"] for x in logged if not x["debt"]), Decimal("0.00"))
    show_total = not (has_debt or mixed)
    reply_text = f"Logged {len(logged)} items" + (f" totaling ₹{fmt(plain_total)}" if show_total else "")
    items_html = ""
    for tx in logged:
        if tx["debt"]:
            items_html += f"""
            <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px dashed var(--border-light, #E8E0D0);">
              <span>🤝 {_esc(tx['summary'])}</span>
            </div>
            """
            continue
        sign = "+" if tx["type"] == "income" else "-"
        color = "#10B981" if tx["type"] == "income" else "#E15554"
        note_html = f" ({_esc(tx['note'])})" if tx.get("note") else ""
        pay_html = f" [{_esc(tx['payment_method'])}]" if tx.get("payment_method") else ""
        items_html += f"""
            <div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px dashed var(--border-light, #E8E0D0);">
              <span>#{tx['id']} <strong>{_esc(tx['category'].capitalize())}</strong>{note_html}{pay_html}</span>
              <span style="font-weight:bold;color:{color};">{sign}₹{fmt(tx['amount'])}</span>
            </div>
            """
    total_html = f" (Total: ₹{fmt(plain_total)})" if show_total else ""
    html_res = f"""
        <div style="margin-bottom:6px;display:flex;gap:6px;align-items:center;">
          <span style="background:#10B981;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">+{len(logged)*15} EXP</span>
          <span style="background:#8B5CF6;color:white;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:bold;">MULTI-LOG ({len(logged)} ITEMS)</span>
        </div>
        <div style="font-weight:bold;font-size:13px;margin-bottom:6px;">Logged {len(logged)} entries{total_html}</div>
        <div style="background:#FAF8F3;border:1px solid var(--border, #D7CDBB);border-radius:6px;padding:8px;font-size:12px;">
          {items_html}
        </div>
        """
    log_chat_message("assistant", html_res)
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
    text = str(data.get("text") or "").strip()
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
    tx_id = None
    if data.get("tx_id") is not None:
        try:
            tx_id = int(data.get("tx_id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "text": "Invalid transaction id."}), 400

    def _totals(extra):
        out = {"balance": float(get_balance()), "today_spent": float(get_today_expense())}
        out.update(extra)
        return jsonify(out)

    if action == "undo":
        group = delete_last_transaction()
        if not group:
            return _totals({"ok": True, "text": "Nothing to undo."})
        n = len(group)
        return _totals({"ok": True, "text": f"Reverted last action ({n} linked entr{'y' if n == 1 else 'ies'})."})

    if action == "delete" and tx_id is not None:
        group = delete_transaction_group_by_id(tx_id)
        if not group:
            resp = _totals({"ok": False, "text": f"No transaction #{tx_id} found."})
            resp.status_code = 404
            return resp
        extra = f" and {len(group) - 1} linked entr{'y' if len(group) == 2 else 'ies'}" if len(group) > 1 else ""
        return _totals({"ok": True, "text": f"Removed #{tx_id}{extra}."})

    if action in ("set_category", "set_payment") and tx_id is not None:
        tx = get_transaction(tx_id)
        if not tx:
            return jsonify({"ok": False, "text": f"No transaction #{tx_id} found."}), 404
        if tx.get("type") not in ("expense", "income"):
            return jsonify({"ok": False, "text": "Transfers have no category or payment method. Delete it and re-enter instead."}), 400

        new_cat = tx.get("category")
        new_pay = tx.get("payment_method")
        if action == "set_category":
            cat = str(data.get("category") or "").strip().lower()
            valid = EXPENSE_CATEGORIES if tx["type"] == "expense" else INCOME_CATEGORIES
            if cat not in valid:
                return jsonify({"ok": False, "text": f"Invalid category. Use one of: {', '.join(valid)}."}), 400
            if not update_transaction(tx_id, category=cat):
                return jsonify({"ok": False, "text": f"No transaction #{tx_id} found."}), 404
            new_cat = cat
            result = {"ok": True, "category": cat, "text": f"Updated category for #{tx_id} to {cat}."}
        else:
            pay = str(data.get("payment_method") or "").strip().lower()
            if pay not in PAYMENT_METHODS:
                return jsonify({"ok": False, "text": f"Invalid payment method. Use one of: {', '.join(PAYMENT_METHODS)}."}), 400
            if not update_transaction(tx_id, payment_method=pay):
                return jsonify({"ok": False, "text": f"No transaction #{tx_id} found."}), 404
            new_pay = pay
            result = {"ok": True, "payment_method": pay, "text": f"Updated payment method for #{tx_id} to {pay}."}

        # A manual correction is explicit human input: trust it as an alias immediately.
        note_key = (tx.get("note") or "").lower().strip()
        _soft_learn_manual(note_key, tx["type"], new_cat, new_pay)
        return _totals(result)

    return jsonify({"ok": False, "text": "Invalid action."}), 400


@app.route("/api/voice", methods=["POST"])
def api_voice():
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"ok": False, "text": "No audio provided."}), 400

    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"ok": False, "text": "The recording was empty. Nothing was logged."}), 400

    transcribed, err = transcribe_voice(audio_bytes, audio_file.filename or "voice.ogg")
    if err:
        # Never invent a transcript: a failed transcription logs nothing.
        return jsonify({"ok": False, "text": err}), 502

    log_chat_message("user", f"🎙 {transcribed}")
    res = _process_text(transcribed, allow_commands=False)
    payload, status = _unwrap(res)
    payload = dict(payload)
    payload["transcribed"] = transcribed
    body_html = payload.get("html") or f"<p>{_esc(payload.get('text', ''))}</p>"
    payload["message_html"] = f"<p class='text-[11px]'>🎙 <em>{_esc(transcribed)}</em></p>{body_html}"
    return jsonify(payload), status


@app.route("/api/export", methods=["GET"])
def api_export():
    rows = _all_transactions(order="id.asc")  # paginated: every row, not just the first 1000
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "type", "amount", "category", "note", "payment_method",
                     "from_bucket", "to_bucket", "person", "pool", "linked_tx_id", "created_at"])
    for r in rows:
        writer.writerow([
            r.get("id"), r.get("type"), r.get("amount"), r.get("category"), r.get("note"),
            r.get("payment_method"), r.get("from_bucket"), r.get("to_bucket"), r.get("person"),
            r.get("pool"), r.get("linked_tx_id"), r.get("created_at"),
        ])

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
    limit = max(1, min(request.args.get("limit", default=50, type=int), 1000))
    payment_method = request.args.get("payment_method")

    if payment_method:
        # filter first, THEN cut to the limit (otherwise the filter can silently under-return)
        rows = [r for r in _all_transactions(order="id.desc") if r.get("payment_method") == payment_method][:limit]
    else:
        rows = _all_transactions(order="id.desc", limit=limit)

    serialised = [
        {
            "id": r["id"],
            "type": r["type"],
            "amount": float(r["amount"]),
            "category": r.get("category") or ("transfer" if r.get("type") == "transfer" else "other"),
            "note": r.get("note"),
            "payment_method": r.get("payment_method"),
            "from_bucket": r.get("from_bucket"),
            "to_bucket": r.get("to_bucket"),
            "created_at": r.get("created_at"),
            "created_at_display": _fmt_dt(r.get("created_at")),
        }
        for r in rows
    ]
    return jsonify(serialised)


@app.route("/api/balances", methods=["GET"])
def api_balances():
    """Per-payment-method balances and the grand total. Uses the same ledger maths as /balance
    (transfers move money between buckets; they are never counted as expenses)."""
    liquid = get_liquid_balances()
    debts = get_debt_balances()
    return jsonify({
        "balances": {k: float(v) for k, v in liquid.items()},
        "total": float(sum(liquid.values(), Decimal("0.00"))),
        "debts": {k: float(v) for k, v in debts.items()},
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
    limit = max(1, min(request.args.get("limit", default=50, type=int), 500))
    return jsonify(get_chat_history(limit=limit))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
