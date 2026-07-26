"""
Fogless — personal expense-tracker bot, serverless webhook version.
Everything lives in one file on purpose: Vercel's Python builder has
inconsistent behavior bundling sibling modules for some function
configurations, so there is nothing here to fail to import.
"""
import os
import io
import csv
import re
import json
import requests
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify

# --------------------------------------------------------------------- config

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
TELEGRAM_SECRET_TOKEN = os.environ["TELEGRAM_SECRET_TOKEN"]
CRON_SECRET = os.environ["CRON_SECRET"]

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_CHAT_API = "https://api.groq.com/openai/v1/chat/completions"
GROQ_AUDIO_API = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_WHISPER_MODEL = "whisper-large-v3-turbo"

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}
SB_TIMEOUT = 8

EXPENSE_CATEGORIES = [
    "food", "groceries", "transport", "bills", "shopping", "entertainment",
    "health", "subscriptions", "rent", "education", "travel", "fitness", "other",
]
INCOME_CATEGORIES = ["salary", "freelance", "gift", "refund", "other"]
PAYMENT_METHODS = ["cash", "upi", "card", "netbanking", "other"]

_CURRENCY_WORDS = {"rs", "rs.", "inr", "rupees", "rupee", "bucks"}

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

app = Flask(__name__)


# ---------------------------------------------------------------- data layer

def _sb_url(path: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{path}"


def add_transaction(tx_type, amount, category, note, payment_method=None) -> int:
    payload = {"type": tx_type, "amount": amount, "category": category, "note": note, "payment_method": payment_method}
    r = requests.post(
        _sb_url("transactions"),
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        json=payload,
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()[0]["id"]


def _all_transactions(order="id.desc", limit=None, since=None):
    params = {"select": "*", "order": order}
    if limit:
        params["limit"] = limit
    if since:
        params["created_at"] = f"gte.{since}"
    r = requests.get(_sb_url("transactions"), headers=SB_HEADERS, params=params, timeout=SB_TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_history(limit=10):
    return _all_transactions(limit=limit)


def get_transaction(tx_id):
    r = requests.get(
        _sb_url("transactions"),
        headers=SB_HEADERS,
        params={"select": "*", "id": f"eq.{tx_id}"},
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def update_transaction(tx_id, **fields) -> bool:
    if not fields:
        return False
    r = requests.patch(
        _sb_url(f"transactions?id=eq.{tx_id}"),
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        json=fields,
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()
    return len(r.json()) > 0


def delete_transaction(tx_id) -> bool:
    r = requests.delete(
        _sb_url(f"transactions?id=eq.{tx_id}"),
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()
    return len(r.json()) > 0


def delete_last_transaction():
    rows = _all_transactions(limit=1)
    if not rows:
        return None
    delete_transaction(rows[0]["id"])
    return rows[0]


BALANCE_BUCKETS = PAYMENT_METHODS + ["unspecified"]  # cash, upi, card, netbanking, other, unspecified


def _settings_key(method: str) -> str:
    return f"starting_balance:{method}"


def get_starting_balances() -> dict:
    r = requests.get(
        _sb_url("settings"),
        headers=SB_HEADERS,
        params={"select": "key,value", "key": "like.starting_balance:*"},
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()
    result = {b: 0.0 for b in BALANCE_BUCKETS}
    for row in r.json():
        method = row["key"].split(":", 1)[1]
        if method in result:
            result[method] = float(row["value"])
    return result


def set_starting_balance(method: str, amount: float):
    r = requests.post(
        _sb_url("settings"),
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
        json={"key": _settings_key(method), "value": str(amount)},
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()


def get_balances_by_method() -> dict:
    rows = _all_transactions()
    result = get_starting_balances()
    for r in rows:
        method = r.get("payment_method") or "unspecified"
        if method not in result:
            result[method] = 0.0
        if r["type"] == "income":
            result[method] += r["amount"]
        else:
            result[method] -= r["amount"]
    return result


def get_balance() -> float:
    return sum(get_balances_by_method().values())


def _since(period):
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "month":
        start = now - timedelta(days=30)
    else:
        return None
    return start.isoformat()


def get_stats(period="month"):
    since = _since(period)
    rows = _all_transactions(since=since) if since else _all_transactions()
    total_income = sum(r["amount"] for r in rows if r["type"] == "income")
    total_expense = sum(r["amount"] for r in rows if r["type"] == "expense")

    by_cat, by_pay = {}, {}
    for r in rows:
        if r["type"] == "expense":
            c = r["category"]
            by_cat.setdefault(c, {"total": 0.0, "cnt": 0})
            by_cat[c]["total"] += r["amount"]
            by_cat[c]["cnt"] += 1

            p = r.get("payment_method") or "unspecified"
            by_pay.setdefault(p, {"total": 0.0, "cnt": 0})
            by_pay[p]["total"] += r["amount"]
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


def get_all_aliases() -> dict:
    r = requests.get(_sb_url("aliases"), headers=SB_HEADERS, params={"select": "*"}, timeout=SB_TIMEOUT)
    r.raise_for_status()
    return {
        row["note_key"]: {
            "type": row["type"], "category": row["category"], "payment_method": row.get("payment_method")
        }
        for row in r.json()
    }


def set_alias(note_key, tx_type, category, payment_method=None):
    r = requests.post(
        _sb_url("aliases"),
        headers={**SB_HEADERS, "Prefer": "resolution=merge-duplicates"},
        json={"note_key": note_key, "type": tx_type, "category": category, "payment_method": payment_method},
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()


def mark_update_processed(update_id) -> bool:
    r = requests.post(
        _sb_url("processed_updates"),
        headers={**SB_HEADERS, "Prefer": "return=representation,resolution=ignore-duplicates"},
        json={"update_id": update_id},
        timeout=SB_TIMEOUT,
    )
    r.raise_for_status()
    return len(r.json()) > 0


def cleanup_old_updates(days=7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    requests.delete(_sb_url(f"processed_updates?created_at=lt.{cutoff}"), headers=SB_HEADERS, timeout=SB_TIMEOUT)


# ------------------------------------------------------------------ nlp layer

def _validate_item(data):
    if not data or data.get("type") not in ("expense", "income"):
        return None
    try:
        amount = float(data["amount"])
    except (TypeError, ValueError, KeyError):
        return None
    if amount <= 0:
        return None
    valid_categories = EXPENSE_CATEGORIES if data["type"] == "expense" else INCOME_CATEGORIES
    category = data.get("category") if data.get("category") in valid_categories else "other"
    payment_method = data.get("payment_method") if data.get("payment_method") in PAYMENT_METHODS else None
    note = data.get("note") or None
    return {"type": data["type"], "amount": amount, "category": category, "payment_method": payment_method, "note": note}


def _call_groq_chat(text):
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
            timeout=20,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception:
        return None

    raw_items = data.get("transactions") if isinstance(data, dict) else None
    if not raw_items:
        return None
    results = [item for item in (_validate_item(x) for x in raw_items) if item is not None]
    return results or None


def quick_parse_single(text, aliases):
    tokens = text.lower().replace("₹", " ").replace(",", " ").split()
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
                val = float(m.group(1))
                if m.group(2) == "k":
                    val *= 1000
                amount = val
                continue
            m2 = re.match(r"^(\d+(?:\.\d+)?)rs$", clean)
            if m2:
                amount = float(m2.group(1))
                continue
        if clean in _CURRENCY_WORDS:
            continue
        if clean in PAYMENT_METHODS:
            payment_method = clean
            continue
        note_tokens.append(clean)

    if amount is None or amount <= 0 or not note_tokens:
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


def transcribe_voice(audio_bytes, filename="voice.ogg"):
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
    except Exception:
        return None


# -------------------------------------------------------------- telegram i/o

def fmt(amount: float) -> str:
    return f"{amount:,.2f}"


def send_message(chat_id, text):
    r = requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=8)
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def send_message_with_keyboard(chat_id, text, keyboard):
    r = requests.post(
        f"{TG_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
        timeout=8,
    )
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def edit_message_text(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TG_API}/editMessageText", json=payload, timeout=8)


def edit_message_reply_markup(chat_id, message_id, reply_markup):
    requests.post(
        f"{TG_API}/editMessageReplyMarkup",
        json={"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
        timeout=8,
    )


def answer_callback_query(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(f"{TG_API}/answerCallbackQuery", json=payload, timeout=8)


def send_document(chat_id, filename, content_bytes, caption=None):
    files = {"document": (filename, content_bytes)}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=15)


def download_telegram_file(file_id):
    try:
        r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=10)
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        r2 = requests.get(file_url, timeout=15)
        r2.raise_for_status()
        return r2.content
    except Exception:
        return None


# --------------------------------------------------------------------- extras

HELP_TEXT = (
    "Just type it — no command needed:\n"
    "  400rs creatine cash\n"
    "  60 milk upi\n"
    "  400 creatine cash, 60 milk upi, 1200 rent upi\n"
    "  got 5000 freelance payment\n"
    "Or send a voice note — same thing, just spoken.\n"
    "After it logs, tap the buttons to fix category/payment or undo.\n\n"
    "Commands (exact, always available):\n"
    "/add <amount> <category> [note] [cash|upi|card|netbanking] - log an expense\n"
    "/income <amount> <source> [note] [cash|upi|card|netbanking] - log income\n"
    "/alias <note words> <category> [cash|upi|card|netbanking] - teach a shortcut\n"
    "/balance [cash|upi|card|netbanking|other] - balance breakdown, or one method\n"
    "/stats [today|week|month|all] - totals + category + payment breakdown\n"
    "/history [n] - last n transactions\n"
    "/undo - remove the most recent transaction\n"
    "/delete <id> - remove a specific transaction\n"
    "/setbalance <cash|upi|card|netbanking|other> <amount> - set a starting balance per method\n"
    "/export - get a CSV of everything\n"
    "/backup - get a full JSON backup\n"
    "/reset confirm - wipe all data\n"
)


def _split_trailing_payment_method(args):
    if args and args[-1].lower() in PAYMENT_METHODS:
        return args[:-1], args[-1].lower()
    return args, None


def _confirmation_text(tx):
    sign = "+" if tx["type"] == "income" else "-"
    bits = [tx["category"]]
    if tx.get("note"):
        bits.append(f"({tx['note']})")
    if tx.get("payment_method"):
        bits.append(f"· {tx['payment_method']}")
    balances = get_balances_by_method()
    total_line = f"Total: {fmt(sum(balances.values()))}"
    if tx.get("payment_method"):
        method = tx["payment_method"]
        total_line = f"{method.capitalize()}: {fmt(balances.get(method, 0.0))}  |  Total: {fmt(sum(balances.values()))}"
    return f"✅ #{tx['id']} {sign}{fmt(tx['amount'])} {' '.join(bits)}\n{total_line}"


def _main_keyboard(tx_id):
    return {
        "inline_keyboard": [[
            {"text": "✏️ Category", "callback_data": f"cc|{tx_id}"},
            {"text": "💳 Payment", "callback_data": f"cp|{tx_id}"},
            {"text": "🗑 Undo", "callback_data": f"ud|{tx_id}"},
        ]]
    }


def _chunk_buttons(items, make_callback_data):
    rows, row = [], []
    for item in items:
        row.append({"text": item, "callback_data": make_callback_data(item)})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _category_keyboard(tx_id, tx_type):
    cats = EXPENSE_CATEGORIES if tx_type == "expense" else INCOME_CATEGORIES
    rows = _chunk_buttons(cats, lambda c: f"sc|{tx_id}|{c}")
    rows.append([{"text": "‹ Back", "callback_data": f"bk|{tx_id}"}])
    return {"inline_keyboard": rows}


def _payment_keyboard(tx_id):
    rows = _chunk_buttons(PAYMENT_METHODS, lambda p: f"sp|{tx_id}|{p}")
    rows.append([{"text": "‹ Back", "callback_data": f"bk|{tx_id}"}])
    return {"inline_keyboard": rows}


# -------------------------------------------------------------------- commands

def handle_command(chat_id, text):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]
    args = parts[1:]

    if cmd in ("/start", "/help"):
        send_message(chat_id, "Bot's live.\n\n" + HELP_TEXT)

    elif cmd == "/add":
        if len(args) < 2:
            return send_message(chat_id, "Usage: /add <amount> <category> [note] [cash|upi|card]")
        try:
            amount = float(args[0])
        except ValueError:
            return send_message(chat_id, "Amount has to be a number.")
        rest, payment_method = _split_trailing_payment_method(args[1:])
        category = rest[0].lower()
        note = " ".join(rest[1:]) if len(rest) > 1 else None
        tx_id = add_transaction("expense", amount, category, note, payment_method)
        tag = f" · {payment_method}" if payment_method else ""
        balances = get_balances_by_method()
        bal_line = f"Total: {fmt(sum(balances.values()))}"
        if payment_method:
            bal_line = f"{payment_method.capitalize()}: {fmt(balances.get(payment_method, 0.0))}  |  Total: {fmt(sum(balances.values()))}"
        send_message(chat_id, f"Logged #{tx_id}: -{fmt(amount)} on {category}{tag}\n{bal_line}")

    elif cmd == "/income":
        if len(args) < 2:
            return send_message(chat_id, "Usage: /income <amount> <source> [note] [cash|upi|card]")
        try:
            amount = float(args[0])
        except ValueError:
            return send_message(chat_id, "Amount has to be a number.")
        rest, payment_method = _split_trailing_payment_method(args[1:])
        source = rest[0].lower()
        note = " ".join(rest[1:]) if len(rest) > 1 else None
        tx_id = add_transaction("income", amount, source, note, payment_method)
        tag = f" · {payment_method}" if payment_method else ""
        balances = get_balances_by_method()
        bal_line = f"Total: {fmt(sum(balances.values()))}"
        if payment_method:
            bal_line = f"{payment_method.capitalize()}: {fmt(balances.get(payment_method, 0.0))}  |  Total: {fmt(sum(balances.values()))}"
        send_message(chat_id, f"Logged #{tx_id}: +{fmt(amount)} from {source}{tag}\n{bal_line}")

    elif cmd == "/alias":
        if len(args) < 2:
            return send_message(chat_id, "Usage: /alias <note words> <category> [cash|upi|card|netbanking]")
        rest, payment_method = _split_trailing_payment_method(args)
        if len(rest) < 2:
            return send_message(chat_id, "Usage: /alias <note words> <category> [cash|upi|card|netbanking]")
        category = rest[-1].lower()
        note_key = " ".join(rest[:-1]).lower()
        if category not in EXPENSE_CATEGORIES and category not in INCOME_CATEGORIES:
            return send_message(chat_id, f"Unknown category '{category}'. Valid: {', '.join(EXPENSE_CATEGORIES)}")
        tx_type = "income" if category in INCOME_CATEGORIES else "expense"
        set_alias(note_key, tx_type, category, payment_method)
        tag = f" ({payment_method})" if payment_method else ""
        send_message(chat_id, f"Learned: '{note_key}' → {category}{tag}")

    elif cmd == "/balance":
        if args:
            method = args[0].lower()
            if method not in PAYMENT_METHODS:
                return send_message(chat_id, f"Unknown payment method '{method}'. Valid: {', '.join(PAYMENT_METHODS)}")
            balances = get_balances_by_method()
            return send_message(chat_id, f"{method.capitalize()} balance: {fmt(balances.get(method, 0.0))}")
        balances = get_balances_by_method()
        lines = ["Balances:"]
        for method in PAYMENT_METHODS + ["unspecified"]:
            if balances.get(method, 0.0) != 0 or method in ("cash", "upi"):
                lines.append(f"  {method}: {fmt(balances.get(method, 0.0))}")
        lines.append(f"\nTotal: {fmt(sum(balances.values()))}")
        send_message(chat_id, "\n".join(lines))

    elif cmd == "/stats":
        period = args[0].lower() if args else "month"
        if period not in ("today", "week", "month", "all"):
            return send_message(chat_id, "Usage: /stats [today|week|month|all]")
        s = get_stats(period)
        lines = [f"Stats ({period}):", f"Income:  +{fmt(s['total_income'])}", f"Expense: -{fmt(s['total_expense'])}", f"Net:     {fmt(s['net'])}"]
        if s["by_category"]:
            lines.append("\nBy category:")
            for row in s["by_category"]:
                lines.append(f"  {row['category']}: {fmt(row['total'])} ({row['cnt']}x)")
        if s["by_payment"]:
            lines.append("\nBy payment method:")
            for row in s["by_payment"]:
                lines.append(f"  {row['payment_method']}: {fmt(row['total'])} ({row['cnt']}x)")
        send_message(chat_id, "\n".join(lines))

    elif cmd == "/history":
        limit = 10
        if args:
            try:
                limit = max(1, min(50, int(args[0])))
            except ValueError:
                pass
        rows = get_history(limit)
        if not rows:
            return send_message(chat_id, "No transactions yet.")
        lines = []
        for r in rows:
            sign = "+" if r["type"] == "income" else "-"
            ts = r["created_at"][:16].replace("T", " ")
            note = f" ({r['note']})" if r.get("note") else ""
            pay = f" [{r['payment_method']}]" if r.get("payment_method") else ""
            lines.append(f"#{r['id']} {ts} {sign}{fmt(r['amount'])} {r['category']}{note}{pay}")
        send_message(chat_id, "\n".join(lines))

    elif cmd == "/undo":
        row = delete_last_transaction()
        if not row:
            return send_message(chat_id, "Nothing to undo.")
        send_message(chat_id, f"Removed #{row['id']}: {row['type']} {fmt(row['amount'])} {row['category']}")

    elif cmd == "/delete":
        if not args:
            return send_message(chat_id, "Usage: /delete <id>")
        try:
            tx_id = int(args[0])
        except ValueError:
            return send_message(chat_id, "ID has to be a number.")
        ok = delete_transaction(tx_id)
        send_message(chat_id, f"Deleted #{tx_id}." if ok else f"No transaction #{tx_id} found.")

    elif cmd == "/setbalance":
        if len(args) < 2:
            return send_message(chat_id, "Usage: /setbalance <cash|upi|card|netbanking|other> <amount>")
        method = args[0].lower()
        if method not in PAYMENT_METHODS:
            return send_message(chat_id, f"Unknown payment method '{method}'. Valid: {', '.join(PAYMENT_METHODS)}")
        try:
            amount = float(args[1])
        except ValueError:
            return send_message(chat_id, "Amount has to be a number.")
        set_starting_balance(method, amount)
        send_message(chat_id, f"Starting {method} balance set to {fmt(amount)}. Total balance: {fmt(get_balance())}")

    elif cmd == "/export":
        rows = get_history(limit=100000)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "type", "amount", "category", "note", "payment_method", "created_at"])
        for r in rows:
            writer.writerow([r["id"], r["type"], r["amount"], r["category"], r.get("note"), r.get("payment_method"), r["created_at"]])
        send_document(chat_id, "expenses_export.csv", buf.getvalue().encode(), "Full export")

    elif cmd == "/backup":
        dump = all_data_dump()
        send_document(chat_id, "expenses_backup.json", json.dumps(dump, indent=2).encode(), "Full backup")

    elif cmd == "/reset":
        if not args or args[0] != "confirm":
            return send_message(chat_id, "This wipes ALL data. To confirm, send: /reset confirm")
        for row in _all_transactions():
            delete_transaction(row["id"])
        for method in PAYMENT_METHODS:
            set_starting_balance(method, 0)
        send_message(chat_id, "All data wiped.")

    else:
        send_message(chat_id, "Unknown command. /help for the list.")


# --------------------------------------------------------------------- freeform

def handle_freeform(chat_id, text):
    aliases = get_all_aliases()
    transactions = parse_multi(text, aliases)
    if not transactions:
        return send_message(
            chat_id,
            "Couldn't tell what that was. Try something like '400 creatine cash' "
            "or use /add <amount> <category> [note]."
        )

    logged = []
    for parsed in transactions:
        tx_id = add_transaction(parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"])
        note_key = (parsed.get("note") or "").lower().strip()
        if note_key and note_key not in aliases:
            set_alias(note_key, parsed["type"], parsed["category"], parsed["payment_method"])
            aliases[note_key] = {"type": parsed["type"], "category": parsed["category"], "payment_method": parsed["payment_method"]}
        logged.append({**parsed, "id": tx_id})

    if len(logged) == 1:
        tx = logged[0]
        send_message_with_keyboard(chat_id, _confirmation_text(tx), _main_keyboard(tx["id"]))
    else:
        lines = [f"✅ Logged {len(logged)} transactions:"]
        for tx in logged:
            sign = "+" if tx["type"] == "income" else "-"
            bits = [tx["category"]]
            if tx["note"]:
                bits.append(f"({tx['note']})")
            if tx["payment_method"]:
                bits.append(f"· {tx['payment_method']}")
            lines.append(f"#{tx['id']} {sign}{fmt(tx['amount'])} {' '.join(bits)}")
        lines.append(f"\nBalance: {fmt(get_balance())}")
        send_message(chat_id, "\n".join(lines))


def handle_voice(chat_id, message):
    voice = message.get("voice") or {}
    file_id = voice.get("file_id")
    if not file_id:
        return
    audio_bytes = download_telegram_file(file_id)
    if not audio_bytes:
        return send_message(chat_id, "Couldn't download that voice note.")
    text = transcribe_voice(audio_bytes)
    if not text:
        return send_message(chat_id, "Couldn't transcribe that voice note. Try typing it instead.")
    handle_freeform(chat_id, text)


# ------------------------------------------------------------------- callbacks

def handle_callback_query(cq):
    user_id = cq.get("from", {}).get("id")
    if user_id != OWNER_ID:
        return answer_callback_query(cq["id"])

    data = cq.get("data", "")
    msg = cq.get("message", {}) or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    parts = data.split("|")
    action = parts[0] if parts else ""

    if action == "cc" and len(parts) == 2:
        tx_id = int(parts[1])
        tx = get_transaction(tx_id)
        if not tx:
            return answer_callback_query(cq["id"], "Transaction not found.")
        edit_message_reply_markup(chat_id, message_id, _category_keyboard(tx_id, tx["type"]))
        return answer_callback_query(cq["id"])

    if action == "cp" and len(parts) == 2:
        tx_id = int(parts[1])
        edit_message_reply_markup(chat_id, message_id, _payment_keyboard(tx_id))
        return answer_callback_query(cq["id"])

    if action == "bk" and len(parts) == 2:
        tx_id = int(parts[1])
        edit_message_reply_markup(chat_id, message_id, _main_keyboard(tx_id))
        return answer_callback_query(cq["id"])

    if action == "ud" and len(parts) == 2:
        tx_id = int(parts[1])
        tx = get_transaction(tx_id)
        delete_transaction(tx_id)
        suffix = f": {tx['type']} {fmt(tx['amount'])} {tx['category']}" if tx else ""
        edit_message_text(chat_id, message_id, f"🗑 Removed #{tx_id}{suffix}")
        return answer_callback_query(cq["id"], "Removed")

    if action == "sc" and len(parts) == 3:
        tx_id, category = int(parts[1]), parts[2]
        update_transaction(tx_id, category=category)
        tx = get_transaction(tx_id)
        if tx and tx.get("note"):
            set_alias(tx["note"].lower().strip(), tx["type"], category, tx.get("payment_method"))
        text = _confirmation_text(tx) if tx else f"#{tx_id} updated"
        edit_message_text(chat_id, message_id, text, reply_markup=_main_keyboard(tx_id))
        return answer_callback_query(cq["id"], f"Category → {category}")

    if action == "sp" and len(parts) == 3:
        tx_id, method = int(parts[1]), parts[2]
        update_transaction(tx_id, payment_method=method)
        tx = get_transaction(tx_id)
        if tx and tx.get("note"):
            set_alias(tx["note"].lower().strip(), tx["type"], tx["category"], method)
        text = _confirmation_text(tx) if tx else f"#{tx_id} updated"
        edit_message_text(chat_id, message_id, text, reply_markup=_main_keyboard(tx_id))
        return answer_callback_query(cq["id"], f"Payment → {method}")

    return answer_callback_query(cq["id"])


# ---------------------------------------------------------------------- routes

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_SECRET_TOKEN:
        return jsonify({"ok": False}), 401

    update = request.get_json(silent=True) or {}

    update_id = update.get("update_id")
    if update_id is not None:
        try:
            if not mark_update_processed(update_id):
                return jsonify({"ok": True})
        except Exception:
            pass

    if "callback_query" in update:
        cq = update["callback_query"]
        try:
            handle_callback_query(cq)
        except Exception:
            answer_callback_query(cq.get("id", ""), "Something went wrong — try again.")
        return jsonify({"ok": True})

    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id")

    if user_id != OWNER_ID:
        return jsonify({"ok": True})

    try:
        if message.get("voice"):
            handle_voice(chat_id, message)
        else:
            text = message.get("text", "")
            if not text:
                return jsonify({"ok": True})
            if text.startswith("/"):
                handle_command(chat_id, text)
            else:
                handle_freeform(chat_id, text)
    except Exception as e:
        send_message(chat_id, f"Something went wrong on that one: {e}")

    return jsonify({"ok": True})


@app.route("/cron/backup", methods=["GET"])
def cron_backup():
    if request.args.get("token") != CRON_SECRET:
        return jsonify({"ok": False}), 401
    dump = all_data_dump()
    send_document(OWNER_ID, "daily_backup.json", json.dumps(dump, indent=2).encode(), "Daily auto-backup")
    cleanup_old_updates()
    return jsonify({"ok": True, "transactions": len(dump["transactions"])})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "expense-bot"})