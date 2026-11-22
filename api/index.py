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
from datetime import date, datetime, timedelta, timezone
from flask import Flask, request, jsonify, send_file, Response, render_template_string, g, has_request_context
import hashlib
import hmac
import secrets
import time
import threading
import traceback
import uuid
from html import escape as _esc, unescape as _unesc
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from werkzeug.exceptions import HTTPException
from werkzeug.utils import safe_join


# --------------------------------------------------------------------- config


def _load_dotenv(path=None):
    """Tiny .env reader (no dependency) so the README's local setup really works. Real environment
    variables always win; it never runs on Vercel or when FOGLESS_NO_DOTENV=1 (the tests set that, so
    they can never pick up your real keys)."""
    if os.getenv("VERCEL") or os.getenv("FOGLESS_NO_DOTENV"):
        return
    path = path or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(path, encoding="utf-8-sig") as fh:
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

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
# Service-role key: used server-side ONLY for database access (every query is scoped to the signed-in
# user inside _sb()) and for deleting an account. It is never sent to the browser.
SUPABASE_KEY = (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
# Anon / publishable key: used for the Supabase Auth endpoints (sign-up, sign-in, refresh, ...).
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()

# Public URL of the deployed site; the password-reset email links back to SITE_URL/reset.html.
# It must be listed under Supabase -> Authentication -> URL Configuration -> Redirect URLs.
SITE_URL = os.getenv("SITE_URL", "").rstrip("/")

# Registration controls. SIGNUPS_OPEN=0 closes registration; SIGNUP_CODE (if set) is an invite code.
SIGNUPS_OPEN = os.getenv("SIGNUPS_OPEN", "1") != "0"
SIGNUP_CODE = os.getenv("SIGNUP_CODE", "")


def _env_int(name, default):
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Per-user message caps (every message can cost a Groq call on YOUR key). 0 disables a cap.
RATE_LIMIT_PER_MIN = _env_int("RATE_LIMIT_PER_MIN", 20)
RATE_LIMIT_PER_DAY = _env_int("RATE_LIMIT_PER_DAY", 300)

# Pre-login abuse limits, per client IP (in-memory, per serverless instance: best effort, but it stops a burst
# from using up Supabase's own sign-in limit, which is shared by EVERY user because sign-in is proxied through
# this server). Wrong-password attempts are counted; successful sign-ins never are.
AUTH_FAILS_PER_5MIN = _env_int("AUTH_FAILS_PER_5MIN", 10)
SIGNUPS_PER_IP_PER_10MIN = _env_int("SIGNUPS_PER_IP_PER_10MIN", 10)
VOICE_PER_MIN = _env_int("VOICE_PER_MIN", 12)  # every upload costs a Whisper call, even if it fails

# Local-only mode: in-memory users and data, no Supabase. Ignored when Supabase is configured or on Vercel.
DEV_MODE = os.getenv("FOGLESS_DEV_MODE", "") == "1"


GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_CHAT_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO_API = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "openai/gpt-oss-120b"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"


def _is_jwt(key: str) -> bool:
    """Legacy anon / service_role keys are JWTs. The newer sb_publishable_... / sb_secret_... keys are NOT:
    Supabase accepts them only in the `apikey` header and rejects the call ("Invalid JWT") if they are also
    sent as `Authorization: Bearer`."""
    return key.startswith("eyJ") and key.count(".") == 2


def _key_headers(key: str) -> dict:
    headers = {"apikey": key, "Content-Type": "application/json"}
    if _is_jwt(key):
        headers["Authorization"] = f"Bearer {key}"
    return headers


SB_HEADERS = _key_headers(SUPABASE_KEY) if SUPABASE_KEY else {}
SB_TIMEOUT = 8
SB_PAGE_SIZE = 1000


# One pooled session for every Supabase call (REST + Auth). Without it each query paid a fresh TCP + TLS
# handshake, which on a message that makes ~10 queries was most of the latency.
_HTTP = requests.Session()
_HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))
_HTTP.mount("http://", requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=16))


IST = timezone(timedelta(hours=5, minutes=30))  # local calendar-day boundary for /stats "today"

# Fallback calendar for users who have no timezone stored (also the original single-user behaviour).
_TZ_NAME_RE = re.compile(r"^[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+){0,2}$")


def _valid_tz(name) -> bool:
    if not isinstance(name, str) or not name or len(name) > 64 or not _TZ_NAME_RE.match(name):
        return False
    try:
        ZoneInfo(name)
        return True
    except Exception:
        return False


def _user_tz():
    """The signed-in user's timezone (IANA name from their account), else IST."""
    name = getattr(g, "tz", None) if has_request_context() else None
    if name and _valid_tz(name):
        return ZoneInfo(name)
    return IST


def _fmt_dt(iso_str) -> str:
    """Render a stored UTC timestamp as a short date/time in the user's timezone, e.g. '16 Sep, 03:42 PM'."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_user_tz()).strftime("%d %b, %I:%M %p")
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


# In-memory storage fallback (FOGLESS_DEV_MODE only): one private store per user id, see _mem().
_MEM = {}
_mem_id_counter = 1


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # Vercel rejects bigger bodies anyway; stops giant uploads locally


def _json_body() -> dict:
    """The request's JSON as a dict, always. A list / string / number body used to crash with AttributeError (HTTP 500)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


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
MAX_ITEMS_PER_MESSAGE = 25  # a runaway LLM answer must not trigger dozens of sequential writes
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
_SYMBOL_RE = re.compile(r"\s*[₹$]\s*")
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
    t = _SYMBOL_RE.sub(" ", text or "")
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


class AuthError(Exception):
    """No signed-in user for something that needs one (including any attempt to touch user data without one)."""


class AuthServiceError(Exception):
    """Supabase Auth is unreachable, not configured, or answered with a 5xx."""


# Every table that holds a user's data. _sb() refuses any other table and scopes these to the signed-in user.
_USER_TABLES = frozenset({"transactions", "aliases", "settings", "pool_contributions", "pool_expenses", "chat_log"})


def _use_sb() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _dev_auth() -> bool:
    """In-memory accounts for local testing. Fails closed: never with Supabase configured, never on Vercel."""
    return DEV_MODE and not _use_sb() and not os.getenv("VERCEL")


def _scope_uid() -> str:
    uid = getattr(g, "uid", None) if has_request_context() else None
    if not uid:
        raise AuthError("no signed-in user")
    return uid


def _mem() -> dict:
    """The signed-in user's private in-memory store (dev mode only)."""
    uid = _scope_uid()
    st = _MEM.get(uid)
    if st is None:
        st = _MEM[uid] = {"tx": [], "aliases": {}, "start": {b: Decimal("0.00") for b in PAYMENT_METHODS},
                          "pc": [], "pe": []}
    return st


def _sb_url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}" if SUPABASE_URL else ""


def _sb_raw(method, path, params=None, json_body=None, prefer=None, ok_statuses=()):
    """The HTTP call to Supabase. Never call this directly: go through _sb() / _sb_rpc(), which scope it."""
    headers = dict(SB_HEADERS)
    if prefer:
        headers["Prefer"] = prefer
    # A reused keep-alive connection can turn out to be dead (idle serverless instance). Reads are safe to
    # repeat once; writes are never retried, so a flaky connection can not create a duplicate row.
    attempts = 2 if method.upper() == "GET" else 1
    for attempt in range(attempts):
        try:
            r = _HTTP.request(
                method, _sb_url(path), headers=headers, params=params, json=json_body, timeout=SB_TIMEOUT
            )
            break
        except requests.RequestException as e:
            if (attempt + 1 < attempts and isinstance(e, requests.ConnectionError)
                    and not isinstance(e, requests.Timeout)):
                continue
            print(f"[supabase] {method} {path} network error: {e}")
            raise StorageError(f"{method} {path}: {type(e).__name__}") from e
    if r.status_code >= 300 and r.status_code not in ok_statuses:
        print(f"[supabase] {method} {path} -> {r.status_code} {r.text[:300]}")
        raise StorageError(f"{method} {path} failed (HTTP {r.status_code})", r.status_code)
    return r


def _sb(method, path, params=None, json_body=None, prefer=None, ok_statuses=()):
    """One place for every Supabase table call -- and the place that enforces tenant isolation.

    The server uses the service-role key, which bypasses Row Level Security, so isolation has to be
    enforced here: every read / update / delete is forced to `user_id = <signed-in user>`, every written
    row is stamped with that user id (overriding anything the caller passed), and a table that is not on
    the allow-list is refused outright. Call sites cannot forget the filter because they never write it.

    Raises StorageError on network errors and on any non-2xx/3xx status (except those in ok_statuses)."""
    table = path.split("?", 1)[0].strip("/").split("/", 1)[0]
    if table not in _USER_TABLES:
        raise StorageError(f"refusing to touch unscoped table {table!r}")
    uid = _scope_uid()
    params = dict(params or {})
    if method.upper() != "POST":  # reads / updates / deletes are filtered; inserts are stamped (below)
        params["user_id"] = f"eq.{uid}"
    if isinstance(json_body, list):
        json_body = [dict(row, user_id=uid) for row in json_body]
    elif isinstance(json_body, dict):
        json_body = dict(json_body, user_id=uid)
    elif json_body is not None:
        raise StorageError("unexpected request body")
    return _sb_raw(method, path, params=params, json_body=json_body, prefer=prefer, ok_statuses=ok_statuses)


# Database functions the app may call. Each takes the user id as p_user_id and only ever totals that user's rows.
_RPC_FUNCS = frozenset({"bucket_balances"})


def _sb_rpc(name):
    """Call one of the allow-listed per-user database functions for the SIGNED-IN user. The user id is
    always injected here; callers cannot pass (or override) it."""
    if name not in _RPC_FUNCS:
        raise StorageError(f"refusing to call unknown function {name!r}")
    return _sb_raw("POST", f"rpc/{name}", json_body={"p_user_id": _scope_uid()})


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

# Per-request scratch space (amount-normalisation notices / negatives)
_tl = threading.local()


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
    _mem()["tx"].append({
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
    _mem()["tx"].append({
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


def add_transactions_bulk(items) -> list:
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


def _all_transactions(order="id.desc", limit=None, since=None, tx_type=None, until=None):
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
            if since and until:  # a repeated key is how PostgREST ANDs two filters on one column
                params["created_at"] = [f"gte.{since}", f"lt.{until}"]
            elif since:
                params["created_at"] = f"gte.{since}"
            elif until:
                params["created_at"] = f"lt.{until}"
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

    rows = list(_mem()["tx"])
    if tx_type:
        rows = [r for r in rows if r["type"] == tx_type]
    if since:
        rows = [r for r in rows if r["created_at"] >= since]
    if until:
        rows = [r for r in rows if r["created_at"] < until]
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
    for r in _mem()["tx"]:
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
    st = _mem()
    before = len(st["tx"])
    st["tx"] = [r for r in st["tx"] if r["id"] != tx_id]
    return len(st["tx"]) < before


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
        found = [r for r in _mem()["tx"] if r["id"] == link_id or r.get("linked_tx_id") == link_id]
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
    st = _mem()
    st["pc"] = [c for c in st["pc"] if c.get("linked_tx_id") not in ids]


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


def _ts(row):
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
    st = _mem()
    src = st["pc"] if table == "pool_contributions" else st["pe"]
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
        return f"Reverted {_pn(row['person'])}'s ₹{fmt(row['amount'])} deposit into {str(row['pool']).upper()} pool.", 1
    if kind == "pe":
        if not delete_pool_expense(row["id"]):
            return None
        return f"Reverted ₹{fmt(row['amount'])} spent from {str(row['pool']).upper()} pool.", 1
    group = delete_transaction_group(row)
    if not group:
        return None
    if len(group) == 1:
        r = group[0]
        if r["type"] == "transfer":
            return f"Reverted #{r['id']}: transfer ₹{fmt(r['amount'])} ({r.get('from_bucket')} → {r.get('to_bucket')}).", 1
        return f"Reverted #{r['id']}: {r['type']} ₹{fmt(r['amount'])} ({r.get('category')}).", 1
    anchor = next((r for r in group if r["type"] != "transfer"), group[0])
    return f"Reverted last action: {len(group)} linked entries (₹{fmt(anchor['amount'])} {anchor['type']}).", len(group)


def delete_pool_contribution(row_id) -> bool:
    if _use_sb():
        r = _sb("DELETE", "pool_contributions", params={"id": f"eq.{row_id}"}, prefer="return=representation")
        return len(_rows(r)) > 0
    st = _mem()
    before = len(st["pc"])
    st["pc"] = [c for c in st["pc"] if c.get("id") != row_id]
    return len(st["pc"]) < before


def delete_pool_expense(row_id) -> bool:
    if _use_sb():
        r = _sb("DELETE", "pool_expenses", params={"id": f"eq.{row_id}"}, prefer="return=representation")
        return len(_rows(r)) > 0
    st = _mem()
    before = len(st["pe"])
    st["pe"] = [e for e in st["pe"] if e.get("id") != row_id]
    return len(st["pe"]) < before


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
    result.update(_mem()["start"])
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
            params={"on_conflict": "user_id,key"},
            json_body={"key": _settings_key(method), "value": str(start_bal)},
            prefer="resolution=merge-duplicates,return=minimal")
        return
    _mem()["start"][method] = start_bal


_RPC = {"ok": None}  # None = not tried yet, False = function not installed (use the slow path)


def _rpc_bucket_balances():
    """Per-user bucket totals from the database (the user id is injected by _sb_rpc, never by the caller)."""
    if not _use_sb() or _RPC["ok"] is False:
        return None
    try:
        rows = _rows(_sb_rpc("bucket_balances"))
    except StorageError as e:
        if e.status in (400, 404):
            _RPC["ok"] = False
            print("[balances] rpc/bucket_balances not installed - run 004_safety_and_speed.sql for faster balances")
            return None
        raise
    _RPC["ok"] = True
    return {r["bucket"]: Decimal(str(r["total"])) for r in rows}


def get_balances_by_method() -> dict:
    """Grand ledger across every bucket — real payment methods AND virtual iou:*/pool:* buckets.
    Transfers move money between two buckets with zero net effect on the total; income/expense
    move it into or out of exactly one bucket, same as before."""
    result = get_starting_balances()
    totals = _rpc_bucket_balances()  # one round trip instead of downloading every row (migration 004)
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
    rows = _all_transactions(since=since, tx_type="expense")
    return sum((Decimal(str(r["amount"])) for r in rows), Decimal("0.00"))


def get_today_flows():
    """(income, expense) logged today in the user's timezone, from a single query. Transfers (moving money
    between your own pouches, IOUs, pools) are neither inflow nor outflow, exactly as in get_today_expense."""
    income = expense = Decimal("0.00")
    for r in _all_transactions(since=_since("today")):
        if r.get("type") == "income":
            income += Decimal(str(r["amount"]))
        elif r.get("type") == "expense":
            expense += Decimal(str(r["amount"]))
    return income, expense


def _since(period):
    now_utc = datetime.now(timezone.utc)
    if period == "today":
        now_ist = now_utc.astimezone(_user_tz())
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
            c = r.get("category") or "other"
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


def _strip_uid(rows):
    return [{k: v for k, v in r.items() if k != "user_id"} for r in rows]


def all_data_dump():
    """The signed-in user's own data, without the internal user_id column."""
    return {
        "transactions": _strip_uid(_all_transactions(order="id.asc")),
        "starting_balances": get_starting_balances(),
        "pool_contributions": _strip_uid(get_pool_contributions()),
        "pool_expenses": _strip_uid(get_pool_expenses()),
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
    return {k: v for k, v in _mem()["aliases"].items() if v.get("confirmed")}


def _get_alias_row(note_key):
    if _use_sb():
        rows = _rows(_sb("GET", "aliases", params={"select": "*", "note_key": f"eq.{note_key}"}))
        return rows[0] if rows else None
    return _mem()["aliases"].get(note_key)


def _put_alias(note_key, tx_type, category, payment_method, confirmed, count):
    if _use_sb():
        _sb("POST", "aliases",
            params={"on_conflict": "user_id,note_key"},
            json_body={
                "note_key": note_key, "type": tx_type, "category": category,
                "payment_method": payment_method, "confirmed": confirmed, "candidate_count": count,
            },
            prefer="resolution=merge-duplicates,return=minimal")
        return
    _mem()["aliases"][note_key] = {
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
    if not _alias_ok(note_key, tx_type) or not category or category == "other":
        return  # "other" is a non-answer; confirming it would make the alias skip the LLM forever
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
    _mem()["pc"].append({
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
    _mem()["pe"].append({
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
    rows = _mem()["pc"]
    return [r for r in rows if r["pool"] == pool_name] if pool_name else list(rows)


def get_pool_expenses(pool_name=None):
    if _use_sb():
        params = {"select": "*"}
        if pool_name:
            params["pool"] = f"eq.{pool_name}"
        return _rows(_sb("GET", "pool_expenses", params=params))
    rows = _mem()["pe"]
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


def reset_all_data(include_settings: bool = False) -> None:
    """Wipes the SIGNED-IN USER'S ledger only: every _sb() call below is scoped to them, so other
    accounts are untouched. include_settings=True also removes non-balance settings (account deletion)."""
    if _use_sb():
        _sb("DELETE", "transactions", params={"id": "gt.0"})
        _sb("DELETE", "pool_contributions", params={"id": "gt.0"})
        _sb("DELETE", "pool_expenses", params={"id": "gt.0"})
        _sb("DELETE", "aliases", params={"note_key": "neq.__none__"})
        _sb("DELETE", "settings",
            params={"key": "neq.__none__" if include_settings else "like.starting_balance:*"})
        _sb("DELETE", "chat_log", params={"id": "gt.0"})
        return
    _MEM.pop(_scope_uid(), None)


# ------------------------------------------------------------------ nlp layer

def _coerce_amount(v) -> Decimal:
    """'1,200' / '₹1200' / 1200 -> Decimal. NaN/Infinity raise (a NaN used to blow up the WHOLE parse)."""
    if isinstance(v, str):
        v = v.replace(",", "").replace("₹", "").strip()
    d = _to_decimal(v)
    if not d.is_finite():
        raise InvalidOperation("non-finite amount")
    return d


def _lc(v):
    return v.strip().lower() if isinstance(v, str) else None


def _validate_item(data):
    if not data or data.get("type") not in ("expense", "income"):
        return None
    try:
        amount = _coerce_amount(data["amount"])
    except (TypeError, ValueError, KeyError, InvalidOperation):
        return None
    if amount <= 0 or amount > MAX_AMOUNT:
        return None
    valid_categories = EXPENSE_CATEGORIES if data["type"] == "expense" else INCOME_CATEGORIES
    category = _lc(data.get("category")) if _lc(data.get("category")) in valid_categories else "other"
    payment_method = _lc(data.get("payment_method")) if _lc(data.get("payment_method")) in PAYMENT_METHODS else None
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
        amount = _coerce_amount(data["amount"])
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
    if len(people) > 20:
        return None  # 20+ sequential writes would blow the serverless time limit
    if share_type == "split" and _to_decimal(amount / (len(people) + 1)) <= 0:
        return None  # my share would round to 0.00

    payment_method = _lc(data.get("payment_method")) if _lc(data.get("payment_method")) in PAYMENT_METHODS else "cash"
    category = _lc(data.get("category")) if _lc(data.get("category")) in EXPENSE_CATEGORIES else "other"
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
        try:
            add_pool_contribution(pool, "me", amount, note, linked_tx_id=tx_id)
        except StorageError:
            try:
                delete_transaction(tx_id)
            except StorageError:
                pass
            raise
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
        if isinstance(raw_items, list) and raw_items:
            results, dropped = [], max(0, len(raw_items) - MAX_ITEMS_PER_MESSAGE)
            for x in raw_items[:MAX_ITEMS_PER_MESSAGE]:
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
                return _demote_unbacked_debt(results, text)
    except Exception as e:
        print(f"Groq API call error: {e}")
    return _fallback_rule_parse(text)


# bare "got" used to flip "300 got milk" / "got haircut 200" into INCOME. "got" now only counts when an
# amount or "paid" follows it ("got 424 upi", "got paid 5000").
_INCOME_RE = re.compile(
    r"\b(?:income|salary|got\s+paid|received|earned|payout|refund|credited|deposited)\b"
    r"|\bgot\s+(?:rs\.?\s*)?[-−]?\d", re.IGNORECASE
)

_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_DATE_RES = [
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MON}\b(?:\s+\d{{4}})?", re.I),   # 12 nov, 5th march 2026
    re.compile(rf"\b{_MON}\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?:,?\s+\d{{4}})?", re.I),  # nov 12
    re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"),                          # 12/11, 12-11-2026
    re.compile(r"\b\d+(?:st|nd|rd|th)\b", re.I),                                       # 5th
]
_QTY_FOR_RE = re.compile(r"\b(\d{1,2})\s+([a-z][a-z]*(?:\s[a-z]+){0,2}?)\s+(?:for|at|@|worth|total)\s+(\d+(?:\.\d+)?k?)\b", re.I)
_QTY_ON_RE = re.compile(r"\b(\d+(?:\.\d+)?k?)\s+(?:on|for)\s+(\d{1,2})\s+([a-z]{3,})\b", re.I)


def _fallback_prep(text):
    """Dates and quantities are not amounts: '450 dinner on 12 nov' used to log a phantom Rs12 expense,
    '3 shirts for 1500' logged Rs3, 'spent 20 on 2 samosas' logged two items."""
    t = text
    for rx in _DATE_RES:
        t = rx.sub(" ", t)
    t = _QTY_FOR_RE.sub(lambda m: f"{m.group(3)} {m.group(2)} x{m.group(1)}", t)
    t = _QTY_ON_RE.sub(lambda m: f"{m.group(1)} {m.group(3)} x{m.group(2)}", t)
    return re.sub(r"[ \t]+", " ", t).strip()

_CATEGORY_WORDS = (
    ("food", ["lunch", "dinner", "breakfast", "food", "cafe", "restaurant", "burger", "pizza", "juice",
              "biscuit", "tea", "coffee", "snack", "chai", "samosa", "dosa", "biryani", "swiggy", "zomato"]),
    ("groceries", ["milk", "groceries", "grocery", "veggies", "vegetables", "fruit", "market"]),
    ("transport", ["uber", "cab", "taxi", "ola", "rapido", "auto", "rickshaw", "bus", "train", "metro", "flight",
                   "transit", "petrol", "diesel", "fuel", "parking", "toll"]),
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

    m = re.search(r"\b(?:split|share)\s+" + N + r"\b(.*?)\bwith\s+(.+)$", tl)  # "split 600 dinner with rahul"
    if m:
        amount, people = _amount_from_str(m.group(1)), _take_people(m.group(3))
        if amount and people:
            pre = m.group(2)
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
    text = _fallback_prep(text)
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
    """Insert one chat turn into the signed-in user's chat_log. Non-fatal: logs on error rather than crashing."""
    if not _use_sb():
        return
    if role == "assistant" and not content.lstrip().startswith("<"):
        content = _esc(content)  # plain-text replies echo user input; the page re-renders history as HTML
    r = None
    for attempt in (1, 2):  # a dropped chat row is a hole in the user's history; a rare duplicate is harmless
        try:
            r = _sb("POST", "chat_log", json_body={"role": role, "content": content}, prefer="return=representation")
            break
        except StorageError as e:
            if attempt == 2:
                print(f"[chat_log] insert failed: {e}")
                return
    try:
        rows = _rows(r)
        if rows and has_request_context():
            g.last_chat_id = max(int(rows[0]["id"]), getattr(g, "last_chat_id", 0))
    except (StorageError, KeyError, TypeError, ValueError):
        pass


def get_chat_history(limit: int = 50, before=None, after=None) -> list:
    """The signed-in user's chat_log rows, oldest-first (chat-display order).
    Default: the newest `limit` rows. before=<id>: the `limit` rows just older than that id (scrolling back).
    after=<id>: rows newer than that id, oldest first (catching up after the app was in the background)."""
    if not _use_sb():
        return []
    try:
        params = {"select": "id,role,content,created_at", "limit": limit}
        if after is not None:
            params.update({"order": "id.asc", "id": f"gt.{max(0, int(after))}"})
            return _rows(_sb("GET", "chat_log", params=params))
        params["order"] = "id.desc"  # ids only grow, so this is chronological and can never tie
        if before:
            params["id"] = f"lt.{int(before)}"
        return list(reversed(_rows(_sb("GET", "chat_log", params=params))))
    except StorageError as e:
        print(f"[chat_log] read failed: {e}")
        return []


def _content_range_total(r) -> int:
    total = (r.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
    return int(total) if total.isdigit() else 0


def _rate_limited():
    """An error message if the signed-in user is over their message caps, else None. Counts rows in
    chat_log (which already records every user message), so the cap holds across serverless instances.
    One query: the newest N timestamps for the per-minute check, plus an exact 24h count for the daily one."""
    if not _use_sb() or (RATE_LIMIT_PER_MIN <= 0 and RATE_LIMIT_PER_DAY <= 0):
        return None
    now = datetime.now(timezone.utc)
    r = _sb("GET", "chat_log",
            params={"select": "created_at", "role": "eq.user",
                    "created_at": f"gte.{(now - timedelta(days=1)).isoformat()}",
                    "order": "created_at.desc", "limit": max(RATE_LIMIT_PER_MIN, 1)},
            prefer="count=exact")
    rows = _rows(r)
    if RATE_LIMIT_PER_DAY > 0 and _content_range_total(r) >= RATE_LIMIT_PER_DAY:
        return "Daily message limit reached. Try again tomorrow."
    if RATE_LIMIT_PER_MIN > 0 and len(rows) >= RATE_LIMIT_PER_MIN:
        try:
            oldest = datetime.fromisoformat(str(rows[-1]["created_at"]).replace("Z", "+00:00"))
            if now - oldest < timedelta(seconds=60):
                return "Slow down a little - too many messages in one minute."
        except (ValueError, KeyError):
            pass
    return None


# --------------------------------------------------------------------- browsing by date (stats / history)
#
# One small grammar is shared by the /stats and /history commands, the in-chat navigator and /api/browse:
#   today  yesterday  week (last 7 days)  month (last 30 days)  thismonth  lastmonth  all
#   2026-11 (a month)   2026-11-20 (a day)   20/11/2026 (day first)   nov / nov 2025 / 14 nov / nov 14
# Day and month boundaries are the signed-in user's own local midnights, not UTC and not IST.


class RangeError(ValueError):
    """The text is not a day, month or period this app understands."""


_RANGE_HELP = ("Try today, yesterday, week, month, thismonth, lastmonth, all, 2026-11, 2026-11-20, nov, nov 2025 "
               "or 14 nov.")
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december")
_MONTH_LOOKUP = {}
for _i, _nm_ in enumerate(_MONTH_NAMES, 1):
    _MONTH_LOOKUP[_nm_] = _i
    _MONTH_LOOKUP[_nm_[:3]] = _i
_MONTH_LOOKUP["sept"] = 9
_RANGE_ALIASES = {"this month": "thismonth", "this-month": "thismonth", "mtd": "thismonth",
                  "last month": "lastmonth", "last-month": "lastmonth", "7d": "week", "30d": "month",
                  "all time": "all", "alltime": "all", "": "month"}
_CAT_ICONS = {"food": "🍽", "groceries": "🛒", "transport": "🚌", "bills": "🧾", "shopping": "🛍",
              "entertainment": "🎬", "health": "💊", "subscriptions": "🔁", "rent": "🏠", "education": "📚",
              "travel": "✈️", "fitness": "🏋️", "salary": "💼", "freelance": "💻", "gift": "🎁",
              "refund": "↩️", "transfer": "↔️"}
PAGE_STEP = 12          # rows a history view shows at a time ("Show 12 more")
MAX_VIEW_ROWS = 300     # hard ceiling for one history view
TREND_MAX_DAYS = 62     # longer spans are charted month by month


def _today_local() -> date:
    return datetime.now(timezone.utc).astimezone(_user_tz()).date()


def _midnight_utc(d: date) -> str:
    """Local midnight at the start of day d, as a UTC timestamp string the database can compare."""
    return datetime(d.year, d.month, d.day, tzinfo=_user_tz()).astimezone(timezone.utc).isoformat()


def _to_local(iso_str):
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_user_tz())
    except Exception:
        return None


def _d_label(d: date, year=True) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


def _span_label(a: date, b: date) -> str:
    return f"{a.day} {a.strftime('%b')} - {b.day} {b.strftime('%b')} {b.year}"


def _next_month(first: date) -> date:
    return date(first.year + (first.month == 12), first.month % 12 + 1, 1)


def _prev_month(first: date) -> date:
    return date(first.year - (first.month == 1), (first.month - 2) % 12 + 1, 1)


def _check_year(y: int):
    if not 2000 <= y <= 2100:
        raise RangeError(f"Year {y} is out of range. " + _RANGE_HELP)


def _canon_day(d: date, today: date) -> str:
    if d == today:
        return "today"
    return "yesterday" if d == today - timedelta(days=1) else d.isoformat()


def _canon_month(first: date, today: date) -> str:
    cur = date(today.year, today.month, 1)
    if first == cur:
        return "thismonth"
    return "lastmonth" if first == _prev_month(cur) else f"{first.year:04d}-{first.month:02d}"


def _day_range(d: date, today: date) -> dict:
    _check_year(d.year)
    tok = _canon_day(d, today)
    return {"kind": "day", "token": tok,
            "title": {"today": "Today", "yesterday": "Yesterday"}.get(tok, _d_label(d)),
            "sub": _d_label(d) if tok in ("today", "yesterday") else "",
            "since": _midnight_utc(d), "until": _midnight_utc(d + timedelta(days=1)),
            "start": d, "end": d, "days": 1,
            "prev": _canon_day(d - timedelta(days=1), today),
            "next": _canon_day(d + timedelta(days=1), today) if d < today else None}


def _month_range(y: int, m: int, today: date) -> dict:
    first = date(y, m, 1)
    _check_year(first.year)
    nxt = _next_month(first)
    last = nxt - timedelta(days=1)
    cur = date(today.year, today.month, 1)
    end = min(last, today) if first <= today else last
    tok = _canon_month(first, today)
    return {"kind": "month", "token": tok,
            "title": {"thismonth": "This month", "lastmonth": "Last month"}.get(
                tok, f"{_MONTH_NAMES[m - 1].capitalize()} {y}"),
            "sub": _span_label(first, end),
            "since": _midnight_utc(first), "until": _midnight_utc(nxt),
            "start": first, "end": end, "days": max(1, (end - first).days + 1),
            "prev": _canon_month(_prev_month(first), today),
            "next": _canon_month(nxt, today) if nxt <= cur else None}


def _rolling_range(kind: str, today: date) -> dict:
    days = 7 if kind == "week" else 30
    since = _since(kind)  # the same rolling window /stats week|month always used
    start = (_to_local(since) or datetime.now(_user_tz())).date()
    return {"kind": "rolling", "token": kind, "title": f"Last {days} days", "sub": _span_label(start, today),
            "since": since, "until": None, "start": start, "end": today, "days": days, "prev": None, "next": None}


def _guess_date(day: int, mon: int, year_str, today: date) -> date:
    if year_str:
        return date(int(year_str), mon, day)
    d = date(today.year, mon, day)
    return d if d <= today else date(today.year - 1, mon, day)


def _parse_range(text=None) -> dict:
    """Turn a period phrase into {kind, token, title, sub, since, until, start, end, days, prev, next}.
    `since` is inclusive, `until` exclusive (UTC timestamps, or None for open-ended). Raises RangeError."""
    t = re.sub(r"\s+", " ", str(text if text is not None else "").strip().lower().replace("_", " "))[:40]
    t = _RANGE_ALIASES.get(t, t)
    today = _today_local()
    cur = date(today.year, today.month, 1)
    if t == "today":
        return _day_range(today, today)
    if t == "yesterday":
        return _day_range(today - timedelta(days=1), today)
    if t in ("week", "month"):
        return _rolling_range(t, today)
    if t == "all":
        return {"kind": "all", "token": "all", "title": "All time", "sub": "", "since": None, "until": None,
                "start": None, "end": today, "days": None, "prev": None, "next": None}
    if t == "thismonth":
        return _month_range(cur.year, cur.month, today)
    if t == "lastmonth":
        p = _prev_month(cur)
        return _month_range(p.year, p.month, today)
    try:
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if m:
            return _day_range(date(int(m[1]), int(m[2]), int(m[3])), today)
        m = re.fullmatch(r"(\d{4})-(\d{1,2})", t)
        if m:
            return _month_range(int(m[1]), int(m[2]), today)
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", t)  # 20/11/2026: day first, the Indian way
        if m:
            return _day_range(date(int(m[3]), int(m[2]), int(m[1])), today)
        m = re.fullmatch(r"(\d{1,2})[/.-](\d{4})", t)
        if m:
            return _month_range(int(m[2]), int(m[1]), today)
        m = re.fullmatch(r"([a-z]{3,9})\.?(?: (\d{4}))?", t)
        if m and m[1] in _MONTH_LOOKUP:
            mon = _MONTH_LOOKUP[m[1]]
            return _month_range(int(m[2]) if m[2] else (today.year if mon <= today.month else today.year - 1),
                                mon, today)
        m = re.fullmatch(r"(\d{1,2}) ([a-z]{3,9})\.?(?: (\d{4}))?", t)
        if m and m[2] in _MONTH_LOOKUP:
            return _day_range(_guess_date(int(m[1]), _MONTH_LOOKUP[m[2]], m[3], today), today)
        m = re.fullmatch(r"([a-z]{3,9})\.? (\d{1,2})(?:,? (\d{4}))?", t)
        if m and m[1] in _MONTH_LOOKUP:
            return _day_range(_guess_date(int(m[2]), _MONTH_LOOKUP[m[1]], m[3], today), today)
    except RangeError:
        raise
    except (ValueError, OverflowError):
        raise RangeError("That is not a real date. " + _RANGE_HELP)
    raise RangeError(f"Could not read {t[:30]!r} as a day or month. " + _RANGE_HELP)


def _bucket_label(b) -> str:
    b = str(b or "?")
    if b.startswith("iou:"):
        return f"{b[4:]} (IOU)"
    if b.startswith("pool:"):
        return f"{b[5:]} pool"
    return b


def _summarize_rows(rows) -> dict:
    """Everything the Stats card shows, from the ledger rows of one period. Transfers never count."""
    zero = Decimal("0.00")
    out = {"income": zero, "expense": zero, "n_in": 0, "n_out": 0, "by_category": [], "by_payment": [],
           "by_income": [], "daily": {}, "biggest": None, "first_day": None}
    cats, pays, incs = {}, {}, {}
    for r in rows:
        kind = r.get("type")
        if kind not in ("income", "expense"):
            continue
        amt = Decimal(str(r["amount"]))
        local = _to_local(r.get("created_at"))
        day = local.date() if local else None
        if day and (out["first_day"] is None or day < out["first_day"]):
            out["first_day"] = day
        if kind == "income":
            out["income"] += amt
            out["n_in"] += 1
            c = incs.setdefault(r.get("category") or "other", {"total": zero, "cnt": 0})
            c["total"] += amt
            c["cnt"] += 1
            continue
        out["expense"] += amt
        out["n_out"] += 1
        c = cats.setdefault(r.get("category") or "other", {"total": zero, "cnt": 0})
        c["total"] += amt
        c["cnt"] += 1
        p = pays.setdefault(r.get("payment_method") or "cash", {"total": zero, "cnt": 0})
        p["total"] += amt
        p["cnt"] += 1
        if day:
            out["daily"][day] = out["daily"].get(day, zero) + amt
        if out["biggest"] is None or amt > out["biggest"][0]:
            out["biggest"] = (amt, r.get("category") or "other", r.get("note"), day)
    out["by_category"] = sorted(({"name": k, **v} for k, v in cats.items()), key=lambda x: -x["total"])
    out["by_payment"] = sorted(({"name": k, **v} for k, v in pays.items()), key=lambda x: -x["total"])
    out["by_income"] = sorted(({"name": k, **v} for k, v in incs.items()), key=lambda x: -x["total"])
    return out


def _trend_points(daily: dict, start: date, end: date):
    """[(label, tooltip, amount)] -- one bar per day for spans up to ~2 months, else one per month."""
    zero = Decimal("0.00")
    if (end - start).days + 1 <= TREND_MAX_DAYS:
        pts, d = [], start
        while d <= end:
            v = daily.get(d, zero)
            pts.append((f"{d.day} {d.strftime('%b')}", f"{_d_label(d, False)} - ₹{fmt(v)}", v))
            d += timedelta(days=1)
        return pts
    months = {}
    for d, v in daily.items():
        months[(d.year, d.month)] = months.get((d.year, d.month), zero) + v
    pts, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        v = months.get((cur.year, cur.month), zero)
        label = f"{cur.strftime('%b')} {cur.year}"
        pts.append((label, f"{label} - ₹{fmt(v)}", v))
        cur = _next_month(cur)
    return pts[-24:]


_NAV_CHIPS = (("today", "Today"), ("yesterday", "Yesterday"), ("week", "7 days"), ("month", "30 days"),
              ("thismonth", "This month"), ("lastmonth", "Last month"), ("all", "All"))


def _nav_html(view: str, rng: dict) -> str:
    """The navigator that sits on top of every Stats / History card: view tabs, quick ranges, step arrows and
    native date / month pickers. Every control is a plain <button data-fl-*>; app.js swaps the card in place."""
    tok = rng["token"]
    tabs = "".join(
        f'<button type="button" class="fl-tab{" fl-on" if v == view else ""}" data-fl-view="{v}" '
        f'data-fl-range="{_esc(tok)}">{lab}</button>'
        for v, lab in (("stats", "📊 Stats"), ("history", "📜 History")))
    chips = "".join(
        f'<button type="button" class="fl-chip{" fl-on" if t == tok else ""}" data-fl-view="{view}" '
        f'data-fl-range="{t}">{"Latest" if (t == "all" and view == "history") else lab}</button>'
        for t, lab in _NAV_CHIPS)
    steppable = rng["kind"] in ("day", "month")
    prev_b = next_b = ""
    if steppable:
        prev_b = (f'<button type="button" class="fl-chip fl-arrow" aria-label="Previous" data-fl-view="{view}" '
                  f'data-fl-range="{_esc(rng["prev"])}">‹</button>')
        next_b = (f'<button type="button" class="fl-chip fl-arrow" aria-label="Next" data-fl-view="{view}" '
                  f'data-fl-range="{_esc(rng["next"])}">›</button>' if rng["next"] else
                  '<button type="button" class="fl-chip fl-arrow fl-off" aria-label="Next" disabled>›</button>')
    today = _today_local()
    pickers = (
        f'<span class="fl-pick-wrap" role="button" tabindex="0" title="Pick a date">📅 Date'
        f'<input type="date" class="fl-pick" data-fl-view="{view}" data-fl-pick="day" max="{today.isoformat()}" '
        f'aria-label="Pick a date" tabindex="-1"></span>'
        f'<span class="fl-pick-wrap" role="button" tabindex="0" title="Pick a month">🗓 Month'
        f'<input type="month" class="fl-pick" data-fl-view="{view}" data-fl-pick="month" '
        f'max="{today.year:04d}-{today.month:02d}" aria-label="Pick a month" tabindex="-1"></span>')
    return (f'<div class="fl-nav"><div class="fl-tabs">{tabs}</div><div class="fl-chips">{chips}</div>'
            f'<div class="fl-step">{prev_b}<span class="fl-cur">{_esc(rng["title"] if rng["kind"] != "all" else "All time")}'
            f'</span>{next_b}{pickers}</div></div>')


def _tile(label, value, cls="") -> str:
    return f'<div class="fl-tile"><span class="fl-k">{label}</span><span class="fl-v {cls}">{value}</span></div>'


def _tiles(income, expense) -> str:
    net = income - expense
    return ('<div class="fl-tiles">'
            + _tile("IN", f"₹{fmt(income)}", "fl-pos") + _tile("OUT", f"₹{fmt(expense)}", "fl-neg")
            + _tile("NET", f"{'+' if net > 0 else '-' if net < 0 else ''}₹{fmt(abs(net))}",
                    "fl-pos" if net > 0 else "fl-neg" if net < 0 else "")
            + '</div>')


def _meter_rows(items, total, tone="") -> str:
    rows = ""
    for it in items[:8]:
        pct = int(round((it["total"] / total) * 100)) if total > 0 else 0
        rows += (f'<div class="fl-mrow"><div class="fl-mtop"><span>{_esc(str(it["name"]).capitalize())} '
                 f'<i>{pct}% · {it["cnt"]}x</i></span><b class="font-numeral">₹{fmt(it["total"])}</b></div>'
                 f'<div class="fl-meter"><div class="fl-fill{tone}" style="width:{max(2, pct)}%"></div></div></div>')
    return rows


def format_stats_html(period="month") -> str:
    rng = _parse_range(period)
    rows = _all_transactions(since=rng["since"], until=rng["until"])
    s = _summarize_rows(rows)
    head = (f'<div class="fl-head"><div class="fl-title">{_esc(rng["title"])} spend</div>'
            f'<div class="fl-sub">{_esc(rng["sub"])}</div></div>')
    wrap = f'<div class="fl-browse" data-fl-view="stats" data-fl-range="{_esc(rng["token"])}">'
    if not (s["n_in"] or s["n_out"]):
        return (f'{wrap}{_nav_html("stats", rng)}<div class="fl-body">{head}'
                '<div class="fl-empty">Nothing logged in this period.</div></div></div>')

    today = _today_local()
    start = rng["start"] or s["first_day"] or today
    end = rng["end"] or today
    span = max(1, (end - start).days + 1)
    facts = [("Transactions", f'{s["n_out"] + s["n_in"]} ({s["n_out"]} out · {s["n_in"]} in)')]
    if s["expense"] > 0:
        facts.append(("Average spend", f'₹{fmt(s["expense"] / span)} per day'))
    if s["biggest"]:
        amt, cat, note, day = s["biggest"]
        what = _esc(str(cat).capitalize()) + (f' ({_esc(str(note))})' if note else "")
        when = f' · {day.day} {day.strftime("%b")}' if day else ""
        facts.append(("Biggest spend", f"₹{fmt(amt)} {what}{when}"))
    if len(s["daily"]) > 1:
        top_day, top_amt = max(s["daily"].items(), key=lambda kv: kv[1])
        facts.append(("Busiest day", f"{_d_label(top_day, False)} · ₹{fmt(top_amt)}"))
    facts_html = '<div class="fl-facts">' + "".join(
        f'<div class="fl-fact"><span>{k}</span><b>{v}</b></div>' for k, v in facts) + "</div>"

    trend_html = ""
    if s["daily"] and span >= 2:
        pts = _trend_points(s["daily"], start, end)
        peak = max((v for _l, _t, v in pts), default=Decimal("0.00"))
        if peak > 0 and len(pts) >= 2:
            bars = "".join(
                f'<div class="fl-col" title="{_esc(tip)}"><div class="fl-bar" '
                f'style="height:{max(3, int(round(v / peak * 100))) if v > 0 else 0}%"></div></div>'
                for _l, tip, v in pts)
            trend_html = (f'<div class="fl-sec"><div class="fl-sec-h">{"Daily" if span <= TREND_MAX_DAYS else "Monthly"}'
                          f' spending</div><div class="fl-trend">{bars}</div>'
                          f'<div class="fl-axis"><span>{_esc(pts[0][0])}</span><span>{_esc(pts[-1][0])}</span></div></div>')

    sections = ""
    if s["by_category"]:
        sections += (f'<div class="fl-sec"><div class="fl-sec-h">Where it went</div>'
                     f'{_meter_rows(s["by_category"], s["expense"])}</div>')
    if s["by_payment"]:
        sections += (f'<div class="fl-sec"><div class="fl-sec-h">Paid with</div>'
                     f'{_meter_rows(s["by_payment"], s["expense"], " fl-alt")}</div>')
    if s["by_income"]:
        sections += (f'<div class="fl-sec"><div class="fl-sec-h">Money in</div>'
                     f'{_meter_rows(s["by_income"], s["income"], " fl-inc")}</div>')
    return (f'{wrap}{_nav_html("stats", rng)}<div class="fl-body">{head}{_tiles(s["income"], s["expense"])}'
            f'{facts_html}{trend_html}{sections}</div></div>')


def _history_row(r) -> str:
    local = _to_local(r.get("created_at"))
    today = _today_local()
    when = ""
    if local:
        d = local.date()
        when = f'{d.day} {d.strftime("%b")}' + (f" {d.year}" if d.year != today.year else "")
        when += f' · {local.strftime("%I:%M %p")}'
    kind = r.get("type")
    note = r.get("note")
    meta = [when] if when else []
    if kind == "transfer":
        icon, label, amt_cls, sign = _CAT_ICONS["transfer"], "Transfer", "fl-neu", ""
        meta.append(f'{_bucket_label(r.get("from_bucket"))} → {_bucket_label(r.get("to_bucket"))}')
    else:
        cat = str(r.get("category") or "other")
        icon, label = _CAT_ICONS.get(cat, "•"), cat.capitalize()
        amt_cls, sign = ("fl-pos", "+") if kind == "income" else ("fl-neg", "-")
        if r.get("payment_method"):
            meta.append(str(r["payment_method"]).upper())
    meta.append(f'#{r["id"]}')
    note_html = f'<span class="fl-note"> · {_esc(str(note))}</span>' if note else ""
    return (f'<div class="fl-row"><div class="fl-ic">{icon}</div><div class="fl-main">'
            f'<div class="fl-l1"><span class="fl-cat">{_esc(label)}</span>{note_html}</div>'
            f'<div class="fl-l2">{_esc(" · ".join(meta))}</div></div>'
            f'<div class="fl-amt font-numeral {amt_cls}">{sign}₹{fmt(r["amount"])}</div></div>')


def format_history_html(limit=12, period="all") -> str:
    limit = max(1, min(int(limit), MAX_VIEW_ROWS))
    rng = _parse_range(period)
    newest_first = lambda rs: sorted(rs, key=lambda r: (str(r.get("created_at") or ""), r["id"]), reverse=True)  # noqa: E731
    if rng["kind"] == "all":  # newest first; fetch one extra row to know whether "Show more" is needed
        fetched = newest_first(_all_transactions(limit=limit + 1))
        rows, has_more, total, day_sums, summary = fetched[:limit], len(fetched) > limit, None, None, None
    else:
        everything = newest_first(_all_transactions(since=rng["since"], until=rng["until"]))
        rows, total = everything[:limit], len(everything)
        has_more = total > limit
        summary = _summarize_rows(everything)
        day_sums = {}
        for r in everything:
            if r.get("type") in ("income", "expense"):
                loc = _to_local(r.get("created_at"))
                if loc:
                    cell = day_sums.setdefault(loc.date(), [Decimal("0.00"), Decimal("0.00")])  # [out, in]
                    cell[1 if r["type"] == "income" else 0] += Decimal(str(r["amount"]))

    title = "Latest transactions" if rng["kind"] == "all" else f'{rng["title"]} · transactions'
    head = (f'<div class="fl-head"><div class="fl-title">{_esc(title)}</div>'
            f'<div class="fl-sub">{_esc(rng["sub"])}</div></div>')
    wrap = f'<div class="fl-browse" data-fl-view="history" data-fl-range="{_esc(rng["token"])}">'
    if not rows:
        return (f'{wrap}{_nav_html("history", rng)}<div class="fl-body">{head}'
                '<div class="fl-empty">No transactions here yet.</div></div></div>')

    today = _today_local()
    body, last_day = "", object()
    for r in rows:
        local = _to_local(r.get("created_at"))
        day = local.date() if local else None
        if day != last_day:
            last_day = day
            if day is None:
                label, sums = "Unknown date", ""
            else:
                prefix = {today: "Today · ", today - timedelta(days=1): "Yesterday · "}.get(day, "")
                label = prefix + _d_label(day, day.year != today.year)
                cell = day_sums.get(day) if day_sums is not None else None
                sums = (f'<span class="fl-day-sum">out ₹{fmt(cell[0])} · in ₹{fmt(cell[1])}</span>'
                        if cell else "")
            body += f'<div class="fl-day"><span>{_esc(label)}</span>{sums}</div>'
        body += _history_row(r)

    if total is not None:
        foot = f'Showing {len(rows)} of {total}'
    else:
        foot = f'Showing the latest {len(rows)}'
    more = ""
    if has_more and limit < MAX_VIEW_ROWS:
        nxt = min(MAX_VIEW_ROWS, limit + PAGE_STEP)
        left = f' · {total - len(rows)} left' if total is not None else ""
        more = (f'<button type="button" class="fl-more" data-fl-view="history" data-fl-range="{_esc(rng["token"])}" '
                f'data-fl-limit="{nxt}">Show {nxt - limit} more{left}</button>')
    tiles = _tiles(summary["income"], summary["expense"]) if summary else ""
    return (f'{wrap}{_nav_html("history", rng)}<div class="fl-body">{head}{tiles}<div class="fl-list">{body}</div>'
            f'<div class="fl-foot"><span>{foot}</span>{more}</div></div></div>')


# --------------------------------------------------------------------- HTML / Web Formatters


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


def _totals(extra=None) -> dict:
    out = _balance_pair()
    out.update(extra or {})
    return out


def _balance_pair() -> dict:
    income, expense = get_today_flows()
    return {"balance": float(get_balance()), "today_spent": float(expense), "today_income": float(income)}


def _unwrap(res):
    """_process_text returns either a dict or a (dict, http_status) tuple (usage errors)."""
    if isinstance(res, tuple):
        return res[0], res[1]
    return res, 200


# ---------------------------------------------------------------------- Authentication (Supabase Auth)
#
# Identity is Supabase Auth (email + password). The backend proxies the sign-in so the browser only ever
# holds two HttpOnly cookies: the access token (short-lived JWT) and the refresh token. Every /api/* call
# validates the access token with Supabase (cached for a few seconds), silently refreshes it when it has
# expired, and then pins g.uid -- the only identity _sb() will ever scope a query to.

ACCESS_COOKIE = "fl_at"
REFRESH_COOKIE = "fl_rt"
_COOKIE_PATH = "/api/"
_COOKIE_SECURE = bool(os.getenv("VERCEL"))  # https on Vercel; plain http works locally
_REFRESH_MAX_AGE = 60 * 60 * 24 * 30

_PUBLIC_API = frozenset({"/api/config", "/api/login", "/api/signup", "/api/logout", "/api/session",
                         "/api/forgot", "/api/reset-password"})
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Dev-mode (no Supabase) account store. See _dev_auth().
_DEV_USERS = {}   # email -> {"id", "salt", "hash", "meta"}
_DEV_TOKENS = {}  # token -> ("access" | "refresh", email, session id)


def _dev_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1).hex()


def _dev_user(email):
    u = _DEV_USERS[email]
    return {"id": u["id"], "email": email, "user_metadata": dict(u["meta"])}


def _dev_session(email, sid=None):
    sid = sid or uuid.uuid4().hex  # a refresh keeps the session id, like GoTrue
    at, rt = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    _DEV_TOKENS[at] = ("access", email, sid)
    _DEV_TOKENS[rt] = ("refresh", email, sid)
    return {"access_token": at, "refresh_token": rt, "token_type": "bearer", "expires_in": 3600,
            "user": _dev_user(email)}


def _gotrue(method, path, token=None, params=None, json_body=None, admin=False):
    """Call Supabase Auth. Returns (status, json_dict). 5xx / network / missing config -> AuthServiceError."""
    key = SUPABASE_KEY if admin else SUPABASE_ANON_KEY
    if not (SUPABASE_URL and key):
        print("[auth] SUPABASE_URL / SUPABASE_ANON_KEY (and SUPABASE_SERVICE_KEY for account deletion) must be set")
        raise AuthServiceError("authentication is not configured")
    headers = _key_headers(key)
    if token:  # a signed-in user's own access token (or a recovery token) always travels as the bearer
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = _HTTP.request(method, f"{SUPABASE_URL}/auth/v1/{path}", headers=headers, params=params,
                          json=json_body, timeout=SB_TIMEOUT)
    except requests.RequestException as e:
        print(f"[auth] {method} {path} network error: {e}")
        raise AuthServiceError(f"{method} {path}: {type(e).__name__}") from e
    try:
        data = r.json() if r.content else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if r.status_code >= 500:
        print(f"[auth] {method} {path} -> {r.status_code}")
        raise AuthServiceError(f"{method} {path} failed (HTTP {r.status_code})")
    return r.status_code, data


def _err_code(d) -> str:
    return str(d.get("error_code") or d.get("error") or "")


def _err_msg(d) -> str:
    return str(d.get("msg") or d.get("error_description") or d.get("message") or "")


# One function per Supabase Auth operation. Each has a real implementation and a dev-mode twin that
# returns the same (status, json) shapes, so the route handlers below have a single code path.

def _auth_signup(email, password, meta):
    if _dev_auth():
        if email in _DEV_USERS:
            return 422, {"error_code": "user_already_exists", "msg": "User already registered"}
        salt = os.urandom(16)
        _DEV_USERS[email] = {"id": str(uuid.uuid4()), "salt": salt, "hash": _dev_hash(password, salt),
                             "meta": dict(meta or {})}
        return 200, _dev_session(email)
    return _gotrue("POST", "signup", json_body={"email": email, "password": password, "data": meta or {}})


def _auth_login(email, password):
    if _dev_auth():
        u = _DEV_USERS.get(email)
        if u and hmac.compare_digest(u["hash"], _dev_hash(password, u["salt"])):
            return 200, _dev_session(email)
        return 400, {"error_code": "invalid_credentials", "msg": "Invalid login credentials"}
    return _gotrue("POST", "token", params={"grant_type": "password"},
                   json_body={"email": email, "password": password})


def _auth_refresh(refresh_token):
    if _dev_auth():
        entry = _DEV_TOKENS.pop(refresh_token, None)
        if entry and entry[0] == "refresh" and entry[1] in _DEV_USERS:
            return 200, _dev_session(entry[1], entry[2])
        return 400, {"error_code": "refresh_token_not_found", "msg": "Invalid Refresh Token"}
    return _gotrue("POST", "token", params={"grant_type": "refresh_token"},
                   json_body={"refresh_token": refresh_token})


def _auth_user(access_token):
    if _dev_auth():
        entry = _DEV_TOKENS.get(access_token)
        if entry and entry[0] == "access" and entry[1] in _DEV_USERS:
            return 200, _dev_user(entry[1])
        return 401, {"error_code": "bad_jwt", "msg": "invalid JWT"}
    return _gotrue("GET", "user", token=access_token)


def _auth_logout(access_token):
    if _dev_auth():
        entry = _DEV_TOKENS.get(access_token)
        if entry:  # revoke this session only, like Supabase's default (scope=local)
            for t in [t for t, e in _DEV_TOKENS.items() if e[2] == entry[2]]:
                _DEV_TOKENS.pop(t, None)
        return 204, {}
    return _gotrue("POST", "logout", token=access_token)


def _auth_recover(email, redirect_to):
    if _dev_auth():
        u = _DEV_USERS.get(email)
        if u:
            at = _dev_session(email)["access_token"]
            print(f"[dev] password reset link for {email}: {redirect_to}#access_token={at}&type=recovery")
        return 200, {}
    return _gotrue("POST", "recover", params={"redirect_to": redirect_to}, json_body={"email": email})


def _auth_update(access_token, body):
    if _dev_auth():
        entry = _DEV_TOKENS.get(access_token)
        if not (entry and entry[0] == "access" and entry[1] in _DEV_USERS):
            return 401, {"error_code": "bad_jwt", "msg": "invalid JWT"}
        u = _DEV_USERS[entry[1]]
        if "password" in body:
            u["salt"] = os.urandom(16)
            u["hash"] = _dev_hash(body["password"], u["salt"])
        if isinstance(body.get("data"), dict):
            u["meta"].update(body["data"])
        return 200, _dev_user(entry[1])
    return _gotrue("PUT", "user", token=access_token, json_body=body)


def _auth_admin_delete(uid):
    if not _UUID_RE.match(uid):
        return 400, {}
    if _dev_auth():
        for email in [e for e, u in _DEV_USERS.items() if u["id"] == uid]:
            _DEV_USERS.pop(email, None)
            for t in [t for t, e in _DEV_TOKENS.items() if e[1] == email]:
                _DEV_TOKENS.pop(t, None)
        return 200, {}
    return _gotrue("DELETE", f"admin/users/{uid}", admin=True)


def _principal(user):
    """The identity we trust for a request, built from Supabase's own answer (never from the client)."""
    if not isinstance(user, dict):
        return None
    uid = str(user.get("id") or "").lower()
    if not _UUID_RE.match(uid):
        return None
    meta = user.get("user_metadata")
    tz = meta.get("timezone") if isinstance(meta, dict) else None
    return {"id": uid, "email": str(user.get("email") or ""), "tz": tz if _valid_tz(tz) else None}


# Verified-token cache: skips a Supabase round-trip for the burst of calls the page makes on load. Short
# on purpose -- it is also how long a signed-out token could keep working on a warm instance.
_TOKEN_CACHE = {}
_TOKEN_CACHE_TTL = 20.0
_TOKEN_CACHE_MAX = 512


def _token_key(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_principal(token, principal):
    if len(_TOKEN_CACHE) >= _TOKEN_CACHE_MAX:
        _TOKEN_CACHE.clear()
    _TOKEN_CACHE[_token_key(token)] = (time.monotonic() + _TOKEN_CACHE_TTL, principal)


def _verify_access_token(token):
    hit = _TOKEN_CACHE.get(_token_key(token))
    if hit and hit[0] > time.monotonic():
        return hit[1]
    status, data = _auth_user(token)
    if status == 200:
        p = _principal(data)
        if p:
            _cache_principal(token, p)
        return p
    if status in (400, 401, 403, 404):
        return None  # expired / forged / revoked
    raise AuthServiceError(f"token check failed (HTTP {status})")


def _authenticate():
    """The signed-in principal for this request, or None. Transparently swaps an expired access token for
    a fresh pair via the refresh token (the new pair is written to cookies by _after_request)."""
    at = request.cookies.get(ACCESS_COOKIE)
    rt = request.cookies.get(REFRESH_COOKIE)
    if at:
        p = _verify_access_token(at)
        if p:
            return p
    if rt:
        status, data = _auth_refresh(rt)
        if status == 200 and data.get("access_token") and data.get("refresh_token"):
            p = _principal(data.get("user"))
            if p:
                g.new_session = data
                _cache_principal(data["access_token"], p)
                return p
        if _err_code(data) == "refresh_token_already_used":
            return None  # a parallel request just rotated it; its response carries the new cookies, keep them
        if status in (200, 400, 401, 403, 422):
            g.clear_cookies = True  # the refresh token is dead; drop the stale cookies
            return None
        raise AuthServiceError(f"refresh failed (HTTP {status})")
    if at:
        g.clear_cookies = True
    return None


def _set_auth_cookies(resp, sess):
    common = dict(httponly=True, secure=_COOKIE_SECURE, samesite="Lax", path=_COOKIE_PATH)
    resp.set_cookie(ACCESS_COOKIE, sess["access_token"], max_age=int(sess.get("expires_in") or 3600), **common)
    resp.set_cookie(REFRESH_COOKIE, sess["refresh_token"], max_age=_REFRESH_MAX_AGE, **common)


def _clear_auth_cookies(resp):
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        resp.delete_cookie(name, path=_COOKIE_PATH, httponly=True, secure=_COOKIE_SECURE, samesite="Lax")


# --------------------------------------------------------------- abuse throttling (in-memory, best effort)

_THROTTLE = {}  # (bucket, key) -> [monotonic timestamps]
_THROTTLE_MAX_KEYS = 4096


def _client_ip() -> str:
    """The caller's IP. On Vercel the platform sets X-Forwarded-For itself (a client-supplied value is replaced)."""
    xff = request.headers.get("X-Forwarded-For", "")
    ip = xff.split(",")[0].strip() if xff else ""
    return (ip or request.remote_addr or "unknown")[:64]


def _throttle_recent(bucket, key, window):
    now = time.monotonic()
    return [t for t in _THROTTLE.get((bucket, key), ()) if now - t < window]


def _throttle_blocked(bucket, key, limit, window) -> bool:
    """True if `key` already has `limit` or more recorded events in the last `window` seconds."""
    return limit > 0 and len(_throttle_recent(bucket, key, window)) >= limit


def _throttle_hit(bucket, key, limit, window) -> bool:
    """Records one event and returns True if `key` is now OVER `limit` events within `window` seconds."""
    events = _throttle_recent(bucket, key, window)
    events.append(time.monotonic())
    if (bucket, key) not in _THROTTLE and len(_THROTTLE) >= _THROTTLE_MAX_KEYS:
        _THROTTLE.clear()  # crude memory bound; an attacker spraying keys only resets everybody's counters
    _THROTTLE[(bucket, key)] = events
    return limit > 0 and len(events) > limit


# Sent with every response from Flask; vercel.json sends the same policy for the static files (a test keeps the two
# identical). The pages use inline <script>, inline style="" and onclick="" attributes, so 'unsafe-inline' is
# required for scripts and styles; everything else is locked to this origin (+ Google Fonts).
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'self'; form-action 'self'; object-src 'none'")
PERMISSIONS_POLICY = "camera=(), microphone=(self), geolocation=(), payment=(), usb=()"


@app.before_request
def _gate():
    """Every /api/* route except the public auth ones needs a valid signed-in user; the identity is
    pinned to g.uid here and nowhere else. Cross-site state-changing requests are refused outright."""
    path = request.path
    if not path.startswith("/api/") or request.method == "OPTIONS":
        return None
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return jsonify({"ok": False, "text": "Cross-site request blocked."}), 403
    if path in _PUBLIC_API:
        return None
    p = _authenticate()
    if not p:
        return jsonify({"ok": False, "auth": False, "text": "Please sign in first."}), 401
    g.uid, g.email, g.tz = p["id"], p["email"], p["tz"]
    return None


@app.after_request
def _after_request(resp):
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"  # per-user data must never be cached or shared
        sess = getattr(g, "new_session", None)
        if sess:
            _set_auth_cookies(resp, sess)
        elif getattr(g, "clear_cookies", False):
            _clear_auth_cookies(resp)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("Permissions-Policy", PERMISSIONS_POLICY)
    return resp


def _norm_email(value) -> str:
    return str(value or "").strip().lower()[:254]


def _pw_problem(password: str, email: str = ""):
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password.encode("utf-8")) > 72:
        return "Password is too long (72 bytes max)."
    if email and password.lower() == email:
        return "Password can't be the same as your email."
    return None


def _remember_tz(access_token, principal, tz):
    """Keep the account's timezone in step with the browser's (drives 'today' and displayed times)."""
    if _valid_tz(tz) and tz != principal.get("tz"):
        try:
            _auth_update(access_token, {"data": {"timezone": tz}})
        except AuthServiceError:
            pass  # cosmetic; never block a sign-in over it


@app.route("/api/config", methods=["GET"])
def api_config():
    return jsonify({"ok": True, "signups_open": SIGNUPS_OPEN, "needs_code": bool(SIGNUP_CODE)})


@app.route("/api/signup", methods=["POST"])
def api_signup():
    if not SIGNUPS_OPEN:
        return jsonify({"ok": False, "text": "Sign-ups are closed."}), 403
    if _throttle_hit("signup", _client_ip(), SIGNUPS_PER_IP_PER_10MIN, 600):
        return jsonify({"ok": False, "text": "Too many attempts. Wait a few minutes and try again."}), 429
    data = _json_body()
    if SIGNUP_CODE and not hmac.compare_digest(str(data.get("code") or "").strip().encode("utf-8"),
                                               SIGNUP_CODE.encode("utf-8")):
        return jsonify({"ok": False, "text": "That invite code isn't valid."}), 403
    email = _norm_email(data.get("email"))
    password = str(data.get("password") or "")
    if not _EMAIL_RE.match(email):
        return jsonify({"ok": False, "text": "Enter a valid email address."}), 400
    problem = _pw_problem(password, email)
    if problem:
        return jsonify({"ok": False, "text": problem}), 400
    tz = data.get("tz")
    status, d = _auth_signup(email, password, {"timezone": tz} if _valid_tz(tz) else {})
    if status == 200:
        if d.get("access_token") and d.get("refresh_token"):  # email confirmation is off: signed in straight away
            if not _principal(d.get("user")):
                raise AuthServiceError("signup returned no usable user")
            g.new_session = d
            return jsonify({"ok": True, "signed_in": True})
        # Confirmation is on. Also what an already-registered address gets, so we never reveal who has an account.
        return jsonify({"ok": True, "signed_in": False,
                        "text": "Almost there - check your inbox to confirm your email, then sign in."})
    code = _err_code(d)
    if code in ("user_already_exists", "email_exists"):
        return jsonify({"ok": False, "text": "An account with that email already exists. Sign in instead."}), 409
    if code == "weak_password":
        return jsonify({"ok": False, "text": _err_msg(d) or "That password is too weak."}), 400
    if code == "signup_disabled":
        return jsonify({"ok": False, "text": "Sign-ups are disabled for this project."}), 403
    if status == 429 or code.startswith("over_"):
        return jsonify({"ok": False, "text": "Too many attempts. Wait a bit and try again."}), 429
    if status in (400, 422):
        return jsonify({"ok": False, "text": _err_msg(d) or "Could not create the account."}), 400
    raise AuthServiceError(f"signup failed (HTTP {status})")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = _json_body()
    email = _norm_email(data.get("email"))
    password = str(data.get("password") or "")
    if not email or not password:
        return jsonify({"ok": False, "text": "Enter your email and password."}), 400
    ip = _client_ip()
    if _throttle_blocked("login", ip, AUTH_FAILS_PER_5MIN, 300):
        return jsonify({"ok": False, "text": "Too many attempts. Wait a few minutes and try again."}), 429
    status, d = _auth_login(email, password)
    if status != 200:
        _throttle_hit("login", ip, AUTH_FAILS_PER_5MIN, 300)
    if status == 200 and d.get("access_token") and d.get("refresh_token"):
        p = _principal(d.get("user"))
        if not p:
            raise AuthServiceError("login returned no usable user")
        _remember_tz(d["access_token"], p, data.get("tz"))
        g.new_session = d
        return jsonify({"ok": True, "email": p["email"]})
    code = _err_code(d)
    if status == 429 or code.startswith("over_"):
        return jsonify({"ok": False, "text": "Too many attempts. Wait a minute and try again."}), 429
    if code == "email_not_confirmed":
        return jsonify({"ok": False, "text": "Confirm your email first - check your inbox for the link."}), 401
    if status in (400, 401, 403, 422):
        return jsonify({"ok": False, "text": "Wrong email or password."}), 401
    raise AuthServiceError(f"login failed (HTTP {status})")


@app.route("/api/logout", methods=["POST"])
def api_logout():
    at = request.cookies.get(ACCESS_COOKIE)
    if at:
        _TOKEN_CACHE.pop(_token_key(at), None)
        try:
            _auth_logout(at)  # revokes the session server-side; the cookies are cleared either way
        except AuthServiceError:
            pass
    g.clear_cookies = True
    return jsonify({"ok": True})


@app.route("/api/session", methods=["GET"])
def api_session():
    p = _authenticate()
    if not p:
        return jsonify({"ok": True, "authenticated": False})
    return jsonify({"ok": True, "authenticated": True, "email": p["email"], "username": p["email"]})


@app.route("/api/forgot", methods=["POST"])
def api_forgot():
    data = _json_body()
    email = _norm_email(data.get("email"))
    if _EMAIL_RE.match(email):
        redirect_to = (SITE_URL or request.host_url.rstrip("/")) + "/reset.html"
        if (_throttle_hit("forgot_ip", _client_ip(), 10, 600) or _throttle_hit("forgot_email", email, 3, 600)):
            pass  # too many requests: send nothing, but answer exactly as usual
        else:
            try:
                _auth_recover(email, redirect_to)
            except AuthServiceError:
                pass  # logged; the answer below is identical either way
    return jsonify({"ok": True, "text": "If that email has an account, a reset link is on its way."})


@app.route("/api/reset-password", methods=["POST"])
def api_reset_password():
    data = _json_body()
    token = str(data.get("access_token") or "")
    password = str(data.get("password") or "")
    if not token:
        return jsonify({"ok": False, "text": "This reset link is invalid. Request a new one."}), 400
    problem = _pw_problem(password)
    if problem:
        return jsonify({"ok": False, "text": problem}), 400
    status, d = _auth_update(token, {"password": password})
    if status == 200:
        _TOKEN_CACHE.pop(_token_key(token), None)
        return jsonify({"ok": True, "text": "Password updated. You can sign in now."})
    code = _err_code(d)
    if code in ("same_password", "weak_password"):
        return jsonify({"ok": False, "text": _err_msg(d) or "Choose a different password."}), 400
    if status in (401, 403):
        return jsonify({"ok": False, "text": "This reset link has expired. Request a new one."}), 401
    if status in (400, 422):
        return jsonify({"ok": False, "text": _err_msg(d) or "Could not update the password."}), 400
    raise AuthServiceError(f"password update failed (HTTP {status})")


@app.route("/api/account/delete", methods=["POST"])
def api_account_delete():
    """Erases the signed-in user's data and then the account itself. Needs the password again."""
    data = _json_body()
    password = str(data.get("password") or "")
    if _throttle_blocked("delete_pw", g.uid, 5, 600):
        return jsonify({"ok": False, "text": "Too many attempts. Wait a few minutes and try again."}), 429
    status, _d = _auth_login(g.email, password) if password else (400, {})
    if status != 200:
        _throttle_hit("delete_pw", g.uid, 5, 600)
        return jsonify({"ok": False, "text": "Wrong password. Nothing was deleted."}), 403
    reset_all_data(include_settings=True)  # explicit, so nothing is orphaned even if a foreign key were missing
    status, _d = _auth_admin_delete(g.uid)
    if status not in (200, 204, 404):
        return jsonify({"ok": False, "text": "Your data was erased but the account itself could not be removed. "
                                              "Try again in a moment."}), 502
    _TOKEN_CACHE.clear()
    g.clear_cookies = True
    return jsonify({"ok": True, "text": "Account deleted."})


@app.errorhandler(AuthError)
def _handle_auth_error(e):
    return jsonify({"ok": False, "auth": False, "text": "Please sign in first."}), 401


@app.errorhandler(AuthServiceError)
def _handle_auth_service_error(e):
    print(f"[auth] {e}")
    return jsonify({"ok": False, "text": "Sign-in is temporarily unavailable. Try again in a moment."}), 503


@app.errorhandler(StorageError)
def _handle_storage_error(e):
    """Supabase is configured but failed: say so loudly instead of pretending everything is fine."""
    print(f"[storage] {e}")
    return jsonify({
        "ok": False,
        "text": "Database error. Check /history before retrying — the last action may not have been saved.",
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
    today_income, today_spent = get_today_flows()
    stats = get_stats("month")
    history = get_history(5)
    return jsonify({
        "ok": True,
        "balance": float(balance),
        "today_spent": float(today_spent),
        "today_income": float(today_income),
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
            return {"ok": True, "text": reply_text, **_balance_pair()}
        args = parts[1:]

        if cmd == "/stats":
            period = " ".join(args).lower() or "month"
            try:
                html = format_stats_html(period)
            except RangeError as e:
                return ({"ok": False, "text": f"Usage: /stats [today|yesterday|week|month|thismonth|lastmonth|all|"
                                              f"YYYY-MM|YYYY-MM-DD]. {e}"}, 400)
            reply_text = f"Stats for {period} period."
            log_chat_message("assistant", html)
            return {"ok": True, "html": html, "text": reply_text, **_balance_pair()}

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
                        **_balance_pair(),
                    }
            html = format_balance_html()
            log_chat_message("assistant", html)
            return {"ok": True, "text": "Balance summary.", "html": html, **_balance_pair()}

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
                **_balance_pair(),
            }
        elif cmd == "/history":
            toks = [a.lower() for a in args]
            limit, period = PAGE_STEP, "all"
            # a leading number is a row count, unless it is a day of the month ("/history 14 nov")
            if toks and re.fullmatch(r"[0-9]{1,3}", toks[0]) and not (len(toks) > 1 and toks[1].rstrip(".") in _MONTH_LOOKUP):
                limit = max(1, min(int(toks.pop(0)), 200))
            if toks:
                period = " ".join(toks)
            try:
                html = format_history_html(limit, period)
            except RangeError as e:
                return ({"ok": False, "text": f"Usage: /history [count] [today|yesterday|week|month|thismonth|"
                                              f"lastmonth|all|YYYY-MM|YYYY-MM-DD]. {e}"}, 400)
            log_chat_message("assistant", html)
            return {"ok": True, "text": f"Last {limit} transactions shown.", "html": html, **_balance_pair()}

        elif cmd == "/undo":
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
                **_balance_pair(),
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
                reply_text = ("⚠ /reset permanently deletes ALL of YOUR transactions, pools, aliases, starting "
                              "balances and chat history. If you really mean it, send: /reset confirm")
                return {"ok": True, "text": reply_text, **_balance_pair()}
            reset_all_data()
            reply_text = "All data reset (database and chat history wiped)." if _use_sb() else "All local data reset."
            log_chat_message("assistant", reply_text)
            return {"ok": True, "text": reply_text, "balance": 0.0, "today_spent": 0.0}

        elif cmd == "/debts":
            html = format_debts_html()
            log_chat_message("assistant", html)
            return {"ok": True, "text": "IOU ledger shown.", "html": html, **_balance_pair()}

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
                    return {"ok": True, "text": reply_text, "html": html, **_balance_pair()}

                pool_cards = []
                for p in pools:
                    s = get_pool_summary(p)
                    pool_cards.append(f"""
                    <div class="border-b border-[#E5DFC9] pb-1 mb-1">
                      <div class="flex justify-between font-bold text-[12px] text-[#142B1A]">
                        <span>⚡ {_esc(p.upper())} Pool</span>
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
                return {"ok": True, "text": reply_text, "html": html, **_balance_pair()}
            s = get_pool_summary(pool_name)
            contrib_lines = "".join(
                f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{_nm(p)}</span><span class='font-numeral text-[10px] font-bold'>+₹{fmt(a)}</span></div>"
                for p, a in s["by_person"].items()
            )
            expense_lines_list = []
            for e in s["expenses"]:
                note_str = f" ({_esc(e['note'])})" if e.get("note") else ""
                expense_lines_list.append(
                    f"<div class='flex justify-between py-0.5 border-b border-[#E5DFC9]'><span>{_esc(str(e.get('category') or 'other').capitalize())}{note_str}</span><span class='font-numeral text-[10px] font-bold text-[#E15554]'>-₹{fmt(e['amount'])}</span></div>"
                )
            expense_lines = "".join(expense_lines_list)
            net_lines = "".join(
                f"<div class='flex justify-between py-0.5'><span class='{'text-[#059669]' if v >= 0 else 'text-[#E15554]'} font-bold'>{_nm(p)}</span><span class='font-numeral text-[10px]'>{'gets back ₹' + fmt(v) if v >= 0 else 'owes ₹' + fmt(abs(v))}</span></div>"
                for p, v in s["net_position"].items()
            )
            html = f"""
            <div class="space-y-2">
              <div class="flex justify-between items-center border-b border-[#A6DCB1] pb-1">
                <span class="font-bold text-[14px] text-[#142B1A]">⚡ {_esc(pool_name.upper())} Pool</span>
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
            return {"ok": True, "text": f"Pool summary for {pool_name}.", "html": html, **_balance_pair()}

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
                        **_balance_pair(),
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
            **_balance_pair(),
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
            **_balance_pair(),
        }

    # One or more items (plain and/or debt) -- each is applied and reported on its own
    logged = []
    plain_items = [p for p in transactions if not p.get("share_type")]
    plain_ids = {}
    if len(plain_items) > 1:
        plain_ids = {id(p): i for p, i in zip(plain_items, add_transactions_bulk(plain_items))}
    for parsed in transactions:
        if parsed.get("share_type"):
            r = _apply_debt_transaction(parsed)
            summary, _ = _debt_reply(parsed, r)
            logged.append({"debt": True, "summary": summary, "amount": parsed["amount"]})
        else:
            tx_id = plain_ids.get(id(parsed)) or add_transaction(
                parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
            note_key = (parsed.get("note") or "").lower().strip()
            if note_key and note_key not in aliases:  # already-confirmed aliases need no extra DB round trips
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
            **_balance_pair(),
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
        **_balance_pair(),
    }


@app.route("/api/message", methods=["POST"])
def api_message():
    data = _json_body()
    raw_text = data.get("text")
    text = raw_text.strip() if isinstance(raw_text, str) else ""  # {"text": 50} / {"text": ["a"]} are not messages
    if not text:
        return jsonify({"ok": False, "text": "Empty message."}), 400

    if len(text) > MAX_TEXT_LEN:  # reject BEFORE anything is stored or counted
        return jsonify({"ok": False, "text": f"Message too long (max {MAX_TEXT_LEN} characters)."}), 400

    limited = _rate_limited()
    if limited:
        return jsonify({"ok": False, "text": limited}), 429

    # Log user turn
    log_chat_message("user", text)

    res = _process_text(text)
    cid = getattr(g, "last_chat_id", None)  # newest chat_log row of this exchange (the page syncs from here)
    if isinstance(res, tuple):
        body = dict(res[0], chat_id=cid) if cid else res[0]
        return jsonify(body), res[1]
    return jsonify(dict(res, chat_id=cid) if cid else res)


@app.route("/api/action", methods=["POST"])
def api_action():
    data = _json_body()
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

    def _totals(extra):
        out = _balance_pair()
        out.update(extra)
        return jsonify(out)

    if action == "undo":
        undone = undo_last_action()
        if not undone:
            return _totals({"ok": True, "text": "Nothing to undo."})
        return _totals({"ok": True, "text": undone[0]})

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

    limited = _rate_limited()
    if limited:
        return jsonify({"ok": False, "text": limited}), 429
    if _throttle_hit("voice", g.uid, VOICE_PER_MIN, 60):
        return jsonify({"ok": False, "text": "Slow down a little - too many voice notes in one minute."}), 429

    audio_bytes = audio_file.read()
    if not audio_bytes:
        return jsonify({"ok": False, "text": "The recording was empty. Nothing was logged."}), 400

    transcribed, err = transcribe_voice(audio_bytes, audio_file.filename or "voice.ogg")
    if err:
        # Never invent a transcript: a failed transcription logs nothing.
        return jsonify({"ok": False, "text": err}), 502

    log_chat_message("user", f"🎙 {transcribed[:MAX_TEXT_LEN]}")
    res = _process_text(transcribed, allow_commands=False)
    payload, status = _unwrap(res)
    payload = dict(payload)
    payload["transcribed"] = transcribed
    if getattr(g, "last_chat_id", None):
        payload["chat_id"] = g.last_chat_id
    body_html = payload.get("html") or f"<p>{_esc(payload.get('text', ''))}</p>"
    payload["message_html"] = f"<p class='text-[11px]'>🎙 <em>{_esc(transcribed)}</em></p>{body_html}"
    return jsonify(payload), status


@app.route("/api/export", methods=["GET"])
def api_export():
    rows = _all_transactions(order="id.asc")  # paginated: every row, not just the first 1000
    buf = io.StringIO()
    writer = csv.writer(buf)

    def _csv_safe(v):
        # a note like "=HYPERLINK(...)" would run as a formula when the CSV is opened in Excel/Sheets
        return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v

    writer.writerow(["id", "type", "amount", "category", "note", "payment_method",
                     "from_bucket", "to_bucket", "person", "pool", "linked_tx_id", "created_at"])
    for r in rows:
        writer.writerow([_csv_safe(c) for c in (
            r.get("id"), r.get("type"), r.get("amount"), r.get("category"), r.get("note"),
            r.get("payment_method"), r.get("from_bucket"), r.get("to_bucket"), r.get("person"),
            r.get("pool"), r.get("linked_tx_id"), r.get("created_at"),
        )])

    output = io.BytesIO()
    output.write(buf.getvalue().encode('utf-8-sig'))  # BOM: Excel shows unicode correctly
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


@app.route("/api/browse", methods=["GET"])
def api_browse():
    """The Stats / History card for any day, month or period, rendered by the same code as /stats and /history.
    The in-chat navigator calls this to swap a card in place without adding messages to the chat."""
    view = request.args.get("view", "stats")
    if view not in ("stats", "history"):
        return jsonify({"ok": False, "text": "view must be stats or history."}), 400
    period = request.args.get("range") or ("month" if view == "stats" else "all")
    limit = max(1, min(request.args.get("limit", default=PAGE_STEP, type=int), MAX_VIEW_ROWS))
    try:
        html = format_stats_html(period) if view == "stats" else format_history_html(limit, period)
    except RangeError as e:
        return jsonify({"ok": False, "text": str(e)}), 400
    return jsonify({"ok": True, "html": html})


@app.route("/api/history", methods=["GET"])
def api_history():
    """Return persisted chat history (user + assistant turns) oldest-first."""
    limit = max(1, min(request.args.get("limit", default=50, type=int), 500))
    return jsonify(get_chat_history(limit=limit, before=request.args.get("before", type=int),
                                    after=request.args.get("after", type=int)))


if DEV_MODE and not _dev_auth():
    print("[warn] FOGLESS_DEV_MODE is ignored: Supabase is configured or this is Vercel.")


if __name__ == "__main__":
    # Local use only. Never expose the Werkzeug debugger on a network interface.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")