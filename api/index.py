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


EXPENSE_CATEGORIES = [
    "food", "groceries", "transport", "bills", "shopping", "entertainment",
    "health", "subscriptions", "rent", "education", "travel", "fitness", "other",
]
INCOME_CATEGORIES = ["salary", "freelance", "gift", "refund", "other"]
PAYMENT_METHODS = ["cash", "upi", "card", "netbanking", "other"]


_CURRENCY_WORDS = {"rs", "rs.", "inr", "rupees", "rupee", "bucks", "$", "gp"}


MULTI_SYSTEM_PROMPT = f"""You are a strict JSON-extraction engine for a personal expense tracker.
A message may describe ONE or SEVERAL transactions (separated by commas, "and", semicolons, or line breaks).
Extract every transaction you find.

Expense categories (pick exactly one per transaction): {", ".join(EXPENSE_CATEGORIES)}
Income categories (pick exactly one per transaction): {", ".join(INCOME_CATEGORIES)}
Payment methods (pick exactly one if mentioned or clearly implied, else null): {", ".join(PAYMENT_METHODS)}

Rules per transaction:
- "type" is "expense" or "income". Assume "expense" unless words like salary, got paid, received, refund, credited clearly signal income.
- "amount" is a plain number (no currency symbols, no commas). "1.5k" or "2k" means 1500 / 2000.
- "category" is exactly one value from the matching list above — pick the closest fit, never invent new categories.
- "payment_method" is exactly one of the listed methods, or null if not mentioned.
- "note" is the short specific detail (e.g. the item/person/service), or null if there isn't one.

If NOTHING in the message describes money changing hands, respond with {{"transactions": []}}.

Respond with ONLY a raw JSON object matching this exact shape, nothing else — no markdown fences, no commentary:
{{"transactions": [{{"type": "expense", "amount": 0, "category": "other", "payment_method": null, "note": null}}]}}
"""


_MULTI_HINT_RE = re.compile(r",|;|\band\b|\n", re.IGNORECASE)


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# In-memory storage fallback when Supabase is not configured
_mem_transactions = []
_mem_id_counter = 1
_mem_aliases = {}
_mem_starting_balances = {b: Decimal("0.00") for b in PAYMENT_METHODS + ["unspecified"]}


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


def delete_last_transaction():
    rows = _all_transactions(limit=1)
    if not rows:
        return None
    delete_transaction(rows[0]["id"])
    return rows[0]


BALANCE_BUCKETS = PAYMENT_METHODS + ["unspecified"]


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
    amount_dec = _to_decimal(amount)
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = requests.post(
                _sb_url("settings"),
                headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
                json={"key": _settings_key(method), "value": str(amount_dec)},
                timeout=SB_TIMEOUT,
            )
            r.raise_for_status()
            return
        except Exception:
            pass
    _mem_starting_balances[method] = amount_dec


def get_balances_by_method() -> dict:
    rows = _all_transactions()
    result = get_starting_balances()
    for r in rows:
        method = r.get("payment_method") or "unspecified"
        if method not in result:
            result[method] = Decimal("0.00")
        if r["type"] == "income":
            result[method] += Decimal(str(r["amount"]))
        else:
            result[method] -= Decimal(str(r["amount"]))
    return result


def get_balance() -> Decimal:
    return sum(get_balances_by_method().values(), Decimal("0.00"))


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

            p = r.get("payment_method") or "unspecified"
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
    return {"transactions": _all_transactions(order="id.asc"), "starting_balances": get_starting_balances()}


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
            results = [item for item in (_validate_item(x) for x in raw_items) if item is not None]
            if results:
                return results
    except Exception as e:
        print(f"Groq API call error: {e}")
    return _fallback_rule_parse(text)


def _fallback_rule_parse(text):
    """Rule-based regex parser when Groq API key is absent or unavailable."""
    tokens = text.lower().replace("₹", " ").replace("$", " ").split()
    if not tokens:
        return None

    tx_type = "expense"
    if any(w in text.lower() for w in ["income", "salary", "got paid", "received", "payout", "refund"]):
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
        if any(w in note_str for w in ["lunch", "dinner", "food", "cafe", "restaurant", "burger", "pizza"]):
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
    return [{"type": tx_type, "amount": _to_decimal(amount), "category": category, "payment_method": payment_method, "note": note}]


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
            <span class="font-numeral text-[10px] text-[#064E3B]">${fmt(c_tot)}</span>
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
        <span class="font-numeral text-[10px] bg-[#142B1A] text-[#FDE047] px-1.5 py-0.5">${fmt(s['total_expense'])} TOTAL</span>
      </div>
      <div class="pixel-window-jrpg p-2 space-y-2.5">
        {rows_html}
      </div>
      <div class="flex items-center justify-between text-[11px] text-[#2D5A35]">
        <span>⚔ Net Pouch: <strong>${fmt(s['net'])}</strong></span>
        <span class="font-bold underline cursor-pointer">Stats Complete ➔</span>
      </div>
    </div>
    """


def format_balance_html() -> str:
    balances = get_balances_by_method()
    total_bal = sum(balances.values(), Decimal("0.00"))
    
    rows_html = ""
    for method in PAYMENT_METHODS:
        amt = balances.get(method, Decimal("0.00"))
        rows_html += f"""
        <div class="flex justify-between py-0.5 border-b border-[#E5DFC9]">
          <span>{method.capitalize()} Pouch</span>
          <span class="font-numeral text-[10px] font-bold text-[#142B1A]">${fmt(amt)}</span>
        </div>
        """

    return f"""
    <div class="space-y-1.5">
      <div class="font-bold text-[13px] text-[#142B1A] flex justify-between">
        <span>Treasury Balances</span>
        <span class="font-numeral text-[9px] text-[#065F46]">${fmt(total_bal)}</span>
      </div>
      <div class="pixel-window-jrpg p-2 text-[12px] space-y-1">
        {rows_html}
      </div>
    </div>
    """


def format_history_html(limit=5) -> str:
    rows = get_history(limit=limit)
    if not rows:
        return "<p class='text-[12px] text-[#6B7280]'>No history items logged yet.</p>"
    
    lines = []
    for r in rows:
        sign = "+" if r["type"] == "income" else "-"
        color = "text-[#059669]" if r["type"] == "income" else "text-[#E15554]"
        note = f" ({r['note']})" if r.get("note") else ""
        pay = f" [{r['payment_method']}]" if r.get("payment_method") else ""
        lines.append(f"<div class='py-0.5 border-b border-[#E5DFC9] flex justify-between'><span>#{r['id']} {r['category']}{note}{pay}</span><span class='font-numeral font-bold {color}'>{sign}${fmt(r['amount'])}</span></div>")

    return f"""
    <div class="space-y-1">
      <div class="font-bold text-[13px] text-[#142B1A]">Last {len(rows)} Logged Items</div>
      <div class="pixel-window-jrpg p-2 text-[11px] space-y-0.5">
        {"".join(lines)}
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
        "history": [{"id": r["id"], "type": r["type"], "amount": float(r["amount"]), "category": r["category"], "note": r.get("note"), "payment_method": r.get("payment_method")} for r in history],
    })


@app.route("/api/message", methods=["POST"])
def api_message():
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "text": "Empty message."}), 400

    # Log user turn
    log_chat_message("user", text)

    # Command handling
    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd == "/stats":
            period = args[0].lower() if args else "month"
            html = format_stats_html(period)
            reply_text = f"Stats for {period} period."
            log_chat_message("assistant", reply_text)
            return jsonify({"ok": True, "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())})

        elif cmd == "/balance":
            html = format_balance_html()
            log_chat_message("assistant", "Balance summary.")
            return jsonify({"ok": True, "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())})

        elif cmd == "/history":
            limit = int(args[0]) if args and args[0].isdigit() else 5
            html = format_history_html(limit)
            log_chat_message("assistant", f"Last {limit} transactions shown.")
            return jsonify({"ok": True, "html": html, "balance": float(get_balance()), "today_spent": float(get_today_expense())})

        elif cmd == "/undo":
            row = delete_last_transaction()
            if not row:
                reply_text = "Nothing to undo."
                log_chat_message("assistant", reply_text)
                return jsonify({"ok": True, "text": reply_text, "balance": float(get_balance()), "today_spent": float(get_today_expense())})
            reply_text = f"Reverted #{row['id']}: {row['type']} ${fmt(row['amount'])} ({row['category']})."
            log_chat_message("assistant", reply_text)
            return jsonify({
                "ok": True,
                "text": f"SPELL: REVERT EXECUTED. Removed #{row['id']}: {row['type']} ${fmt(row['amount'])} ({row['category']}).",
                "html": f"<p>Reverted <strong>#{row['id']}</strong> (${fmt(row['amount'])}) back to the treasury purse.</p>",
                "balance": float(get_balance()),
                "today_spent": float(get_today_expense()),
            })

        elif cmd == "/delete":
            if not args or not args[0].isdigit():
                return jsonify({"ok": False, "text": "Usage: /delete <id>"}), 400
            tx_id = int(args[0])
            ok = delete_transaction(tx_id)
            reply_text = f"Deleted #{tx_id}." if ok else f"No transaction #{tx_id} found."
            log_chat_message("assistant", reply_text)
            return jsonify({
                "ok": True,
                "text": reply_text,
                "balance": float(get_balance()),
                "today_spent": float(get_today_expense()),
            })

        elif cmd == "/aliases":
            aliases = get_all_aliases()
            if not aliases:
                reply_text = "No aliases learned yet."
                log_chat_message("assistant", reply_text)
                return jsonify({"ok": True, "text": reply_text})
            lines = [f"'{k}' -> {v['category']} [{v.get('payment_method') or 'any'}]" for k, v in aliases.items()]
            reply_text = "Learned Aliases:\n" + "\n".join(lines)
            log_chat_message("assistant", reply_text)
            return jsonify({"ok": True, "text": reply_text})

        elif cmd == "/reset":
            global _mem_transactions
            _mem_transactions.clear()
            log_chat_message("assistant", "All local data reset.")
            return jsonify({"ok": True, "text": "All local data reset!", "balance": 0.0, "today_spent": 0.0})

    # Freeform text parsing
    aliases = get_all_aliases()
    transactions = parse_multi(text, aliases)
    if not transactions:
        reply_text = f"Could not parse amount from '{text}'. Hint: type coins first, e.g. 12 notebook cash"
        log_chat_message("assistant", reply_text)
        return jsonify({
            "ok": True,
            "text": reply_text,
            "html": f"<p>Could not parse amount from <span class='bg-[#F87171] text-white px-1 text-[13px] font-mono'>\"{text}\"</span>. Hint: type the coins first, e.g. <strong class='underline decoration-2'>12 notebook cash</strong>.</p>",
            "balance": float(get_balance()),
            "today_spent": float(get_today_expense()),
        })

    logged = []
    for parsed in transactions:
        tx_id = add_transaction(parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
        note_key = (parsed.get("note") or "").lower().strip()
        learn_alias_auto(note_key, parsed["type"], parsed["category"], parsed["payment_method"])
        logged.append({**parsed, "id": tx_id})

    last_tx = logged[0]
    sign = "+" if last_tx["type"] == "income" else "-"
    badge_bg = "bg-[#10B981]" if last_tx["type"] == "income" else "bg-[#E15554]"
    reply_text = f"Logged ${fmt(last_tx['amount'])} on {last_tx['category']}"
    log_chat_message("assistant", reply_text)
    html_res = f"""
    <div class="flex items-center gap-1.5 mb-1 flex-wrap">
      <span class="{badge_bg} text-white px-1.5 py-0.5 font-numeral text-[8px] font-bold border border-[#7F1D1D]">{sign}${fmt(last_tx['amount'])} GP</span>
      <span class="bg-[#10B981] text-white px-1.5 py-0.5 font-numeral text-[8px] font-bold border border-[#065F46]">+15 EXP</span>
      <span class="bg-[#3B82F6] text-white px-1.5 py-0.5 font-numeral text-[8px] font-bold border border-[#1E40AF]">{last_tx['category'].upper()}</span>
    </div>
    <p>Logged <strong class="text-[#0E2014] font-numeral text-[12px]">${fmt(last_tx['amount'])}</strong> under <strong class="underline decoration-2">{last_tx['category'].capitalize()}</strong>{f" via {last_tx['payment_method'].upper()}" if last_tx.get('payment_method') else ""}.</p>
    """

    return jsonify({
        "ok": True,
        "html": html_res,
        "text": f"Logged ${fmt(last_tx['amount'])} on {last_tx['category']}",
        "tx_id": last_tx["id"],
        "category": last_tx["category"],
        "payment_method": last_tx.get("payment_method"),
        "balance": float(get_balance()),
        "today_spent": float(get_today_expense()),
    })


@app.route("/api/action", methods=["POST"])
def api_action():
    data = request.get_json(silent=True) or {}
    action = data.get("action")
    tx_id = data.get("tx_id")

    if action == "undo":
        row = delete_last_transaction()
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
        "text": f"Logged voice note: ${fmt(last_tx['amount'])} on {last_tx['category']}",
        "message_html": f"<p>Deciphered voice memo: <em>\"{transcribed}\"</em>! Logged <strong>${fmt(last_tx['amount'])}</strong> under <strong>{last_tx['category']}</strong>.</p>",
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
        balances = {m: float(_mem_starting_balances.get(m, 0)) for m in PAYMENT_METHODS + ["unspecified"]}

    # Apply all transactions
    rows = _all_transactions()
    for tx in rows:
        method = tx.get("payment_method") or "unspecified"
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


# --------------------------------------------------------------------- Legacy Telegram Webhook Handler


@app.route("/webhook", methods=["POST"])
def webhook():
    if TELEGRAM_SECRET_TOKEN and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_SECRET_TOKEN:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "notice": "Telegram connection dropped in favor of web app."})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
