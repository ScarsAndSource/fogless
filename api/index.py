"""
Personal expense-tracker bot — serverless webhook version.
Runs as a Vercel Python function. No persistent process, no polling loop —
Telegram POSTs each update straight to /webhook, and the function replies
and exits.

Ways to log:
  1. Type it: "400rs creatine cash", "60 milk upi", "got 5000 freelance"
     - even several at once: "400 creatine cash, 60 milk upi, 1200 rent upi"
  2. Send a voice note — it gets transcribed and parsed the same way.
  3. Commands, always available as an exact fallback (see /help).

After logging a single transaction, tap the buttons under the confirmation
to fix category/payment method, or undo it — no retyping needed. Corrections
teach the bot: next time you mention the same item, it's instant and free.
"""
import os
import io
import csv
import json
import requests
from flask import Flask, request, jsonify

import supa
import nlp


BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
TELEGRAM_SECRET_TOKEN = os.environ["TELEGRAM_SECRET_TOKEN"]
CRON_SECRET = os.environ["CRON_SECRET"]


TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


app = Flask(__name__)


def fmt(amount: float) -> str:
    return f"{amount:,.2f}"


# ------------------------------------------------------------- Telegram calls


def send_message(chat_id: int, text: str) -> int | None:
    r = requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=8)
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def send_message_with_keyboard(chat_id: int, text: str, keyboard: dict) -> int | None:
    r = requests.post(
        f"{TG_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
        timeout=8,
    )
    try:
        return r.json()["result"]["message_id"]
    except Exception:
        return None


def edit_message_text(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TG_API}/editMessageText", json=payload, timeout=8)


def edit_message_reply_markup(chat_id: int, message_id: int, reply_markup: dict):
    requests.post(
        f"{TG_API}/editMessageReplyMarkup",
        json={"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
        timeout=8,
    )


def answer_callback_query(callback_query_id: str, text: str = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(f"{TG_API}/answerCallbackQuery", json=payload, timeout=8)


def send_document(chat_id: int, filename: str, content_bytes: bytes, caption: str = None):
    files = {"document": (filename, content_bytes)}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=15)


def download_telegram_file(file_id: str) -> bytes | None:
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
    "/alias <note> <category> [cash|upi|card|netbanking] - teach a shortcut\n"
    "/balance - current balance\n"
    "/stats [today|week|month|all] - totals + category + payment breakdown\n"
    "/history [n] - last n transactions\n"
    "/undo - remove the most recent transaction\n"
    "/delete <id> - remove a specific transaction\n"
    "/setbalance <amount> - set your starting balance\n"
    "/export - get a CSV of everything\n"
    "/backup - get a full JSON backup\n"
    "/reset confirm - wipe all data\n"
)


def _split_trailing_payment_method(args: list[str]):
    if args and args[-1].lower() in nlp.PAYMENT_METHODS:
        return args[:-1], args[-1].lower()
    return args, None


def _confirmation_text(tx: dict) -> str:
    sign = "+" if tx["type"] == "income" else "-"
    bits = [tx["category"]]
    if tx.get("note"):
        bits.append(f"({tx['note']})")
    if tx.get("payment_method"):
        bits.append(f"· {tx['payment_method']}")
    return f"Logged #{tx['id']}: {sign}{fmt(tx['amount'])} {' '.join(bits)}\nBalance: {fmt(supa.get_balance())}"


def _main_keyboard(tx_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Category", "callback_data": f"cc|{tx_id}"},
            {"text": "Payment", "callback_data": f"cp|{tx_id}"},
            {"text": "Undo", "callback_data": f"ud|{tx_id}"},
        ]]
    }


def _chunk_buttons(items: list[str], make_callback_data) -> list[list[dict]]:
    rows, row = [], []
    for item in items:
        row.append({"text": item, "callback_data": make_callback_data(item)})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _category_keyboard(tx_id: int, tx_type: str) -> dict:
    cats = nlp.EXPENSE_CATEGORIES if tx_type == "expense" else nlp.INCOME_CATEGORIES
    rows = _chunk_buttons(cats, lambda c: f"sc|{tx_id}|{c}")
    rows.append([{"text": "Back", "callback_data": f"bk|{tx_id}"}])
    return {"inline_keyboard": rows}


def _payment_keyboard(tx_id: int) -> dict:
    rows = _chunk_buttons(nlp.PAYMENT_METHODS, lambda p: f"sp|{tx_id}|{p}")
    rows.append([{"text": "Back", "callback_data": f"bk|{tx_id}"}])
    return {"inline_keyboard": rows}


# -------------------------------------------------------------------- commands


def handle_command(chat_id: int, text: str):
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
        tx_id = supa.add_transaction("expense", amount, category, note, payment_method)
        tag = f" · {payment_method}" if payment_method else ""
        send_message(chat_id, f"Logged #{tx_id}: -{fmt(amount)} on {category}{tag}\nBalance: {fmt(supa.get_balance())}")

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
        tx_id = supa.add_transaction("income", amount, source, note, payment_method)
        tag = f" · {payment_method}" if payment_method else ""
        send_message(chat_id, f"Logged #{tx_id}: +{fmt(amount)} from {source}{tag}\nBalance: {fmt(supa.get_balance())}")

    elif cmd == "/alias":
        if len(args) < 2:
            return send_message(chat_id, "Usage: /alias <note words> <category> [cash|upi|card|netbanking]")
        rest, payment_method = _split_trailing_payment_method(args)
        if len(rest) < 2:
            return send_message(chat_id, "Usage: /alias <note words> <category> [cash|upi|card|netbanking]")
        category = rest[-1].lower()
        note_key = " ".join(rest[:-1]).lower()
        if category not in nlp.EXPENSE_CATEGORIES and category not in nlp.INCOME_CATEGORIES:
            return send_message(chat_id, f"Unknown category '{category}'. Valid: {', '.join(nlp.EXPENSE_CATEGORIES)}")
        tx_type = "income" if category in nlp.INCOME_CATEGORIES else "expense"
        supa.set_alias(note_key, tx_type, category, payment_method)
        tag = f" ({payment_method})" if payment_method else ""
        send_message(chat_id, f"Learned: '{note_key}' -> {category}{tag}")

    elif cmd == "/balance":
        send_message(chat_id, f"Balance: {fmt(supa.get_balance())}")

    elif cmd == "/stats":
        period = args[0].lower() if args else "month"
        if period not in ("today", "week", "month", "all"):
            return send_message(chat_id, "Usage: /stats [today|week|month|all]")
        s = supa.get_stats(period)
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
        rows = supa.get_history(limit)
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
        row = supa.delete_last_transaction()
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
        ok = supa.delete_transaction(tx_id)
        send_message(chat_id, f"Deleted #{tx_id}." if ok else f"No transaction #{tx_id} found.")

    elif cmd == "/setbalance":
        if not args:
            return send_message(chat_id, "Usage: /setbalance <amount>")
        try:
            amount = float(args[0])
        except ValueError:
            return send_message(chat_id, "Amount has to be a number.")
        supa.set_starting_balance(amount)
        send_message(chat_id, f"Starting balance set to {fmt(amount)}. Balance: {fmt(supa.get_balance())}")

    elif cmd == "/export":
        rows = supa.get_history(limit=100000)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "type", "amount", "category", "note", "payment_method", "created_at"])
        for r in rows:
            writer.writerow([r["id"], r["type"], r["amount"], r["category"], r.get("note"), r.get("payment_method"), r["created_at"]])
        send_document(chat_id, "expenses_export.csv", buf.getvalue().encode(), "Full export")

    elif cmd == "/backup":
        dump = supa.all_data_dump()
        send_document(chat_id, "expenses_backup.json", json.dumps(dump, indent=2).encode(), "Full backup")

    elif cmd == "/reset":
        if not args or args[0] != "confirm":
            return send_message(chat_id, "This wipes ALL data. To confirm, send: /reset confirm")
        for row in supa._all_transactions():
            supa.delete_transaction(row["id"])
        supa.set_starting_balance(0)
        send_message(chat_id, "All data wiped.")

    else:
        send_message(chat_id, "Unknown command. /help for the list.")


# --------------------------------------------------------------------- freeform


def handle_freeform(chat_id: int, text: str):
    aliases = supa.get_all_aliases()
    transactions = nlp.parse_multi(text, aliases)
    if not transactions:
        return send_message(
            chat_id,
            "Couldn't tell what that was. Try something like '400 creatine cash' "
            "or use /add <amount> <category> [note]."
        )

    logged = []
    for parsed in transactions:
        tx_id = supa.add_transaction(
            parsed["type"], parsed["amount"], parsed["category"], parsed["note"], parsed["payment_method"]
        )
        note_key = (parsed.get("note") or "").lower().strip()
        if note_key and note_key not in aliases:
            supa.set_alias(note_key, parsed["type"], parsed["category"], parsed["payment_method"])
            aliases[note_key] = {
                "type": parsed["type"], "category": parsed["category"], "payment_method": parsed["payment_method"]
            }
        logged.append({**parsed, "id": tx_id})

    if len(logged) == 1:
        tx = logged[0]
        send_message_with_keyboard(chat_id, _confirmation_text(tx), _main_keyboard(tx["id"]))
    else:
        lines = [f"Logged {len(logged)} transactions:"]
        for tx in logged:
            sign = "+" if tx["type"] == "income" else "-"
            bits = [tx["category"]]
            if tx["note"]:
                bits.append(f"({tx['note']})")
            if tx["payment_method"]:
                bits.append(f"· {tx['payment_method']}")
            lines.append(f"#{tx['id']} {sign}{fmt(tx['amount'])} {' '.join(bits)}")
        lines.append(f"\nBalance: {fmt(supa.get_balance())}")
        send_message(chat_id, "\n".join(lines))


def handle_voice(chat_id: int, message: dict):
    voice = message.get("voice") or {}
    file_id = voice.get("file_id")
    if not file_id:
        return
    audio_bytes = download_telegram_file(file_id)
    if not audio_bytes:
        return send_message(chat_id, "Couldn't download that voice note.")
    text = nlp.transcribe_voice(audio_bytes)
    if not text:
        return send_message(chat_id, "Couldn't transcribe that voice note. Try typing it instead.")
    handle_freeform(chat_id, text)


# ------------------------------------------------------------------- callbacks


def handle_callback_query(cq: dict):
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
        tx = supa.get_transaction(tx_id)
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
        tx = supa.get_transaction(tx_id)
        supa.delete_transaction(tx_id)
        suffix = f": {tx['type']} {fmt(tx['amount'])} {tx['category']}" if tx else ""
        edit_message_text(chat_id, message_id, f"Removed #{tx_id}{suffix}")
        return answer_callback_query(cq["id"], "Removed")

    if action == "sc" and len(parts) == 3:
        tx_id, category = int(parts[1]), parts[2]
        supa.update_transaction(tx_id, category=category)
        tx = supa.get_transaction(tx_id)
        if tx and tx.get("note"):
            supa.set_alias(tx["note"].lower().strip(), tx["type"], category, tx.get("payment_method"))
        text = _confirmation_text(tx) if tx else f"#{tx_id} updated"
        edit_message_text(chat_id, message_id, text, reply_markup=_main_keyboard(tx_id))
        return answer_callback_query(cq["id"], f"Category -> {category}")

    if action == "sp" and len(parts) == 3:
        tx_id, method = int(parts[1]), parts[2]
        supa.update_transaction(tx_id, payment_method=method)
        tx = supa.get_transaction(tx_id)
        if tx and tx.get("note"):
            supa.set_alias(tx["note"].lower().strip(), tx["type"], tx["category"], method)
        text = _confirmation_text(tx) if tx else f"#{tx_id} updated"
        edit_message_text(chat_id, message_id, text, reply_markup=_main_keyboard(tx_id))
        return answer_callback_query(cq["id"], f"Payment -> {method}")

    return answer_callback_query(cq["id"])


# ---------------------------------------------------------------------- routes


@app.route("/webhook", methods=["POST"])
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_SECRET_TOKEN:
        return jsonify({"ok": False}), 401

    update = request.get_json(silent=True) or {}

    update_id = update.get("update_id")
    if update_id is not None and not supa.mark_update_processed(update_id):
        return jsonify({"ok": True})  # Telegram retried a delivery we already handled

    if "callback_query" in update:
        cq = update["callback_query"]
        try:
            handle_callback_query(cq)
        except Exception:
            answer_callback_query(cq.get("id", ""), "Something went wrong -- try again.")
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
    """
    Hit once a day by a GitHub Actions workflow. Does three jobs at once:
    1. Touches the Supabase DB so the free project never hits its inactivity pause.
    2. Sends a fresh JSON backup to your own Telegram chat as a safety net.
    3. Cleans up old idempotency records so that table doesn't grow forever.
    """
    if request.args.get("token") != CRON_SECRET:
        return jsonify({"ok": False}), 401

    dump = supa.all_data_dump()
    send_document(OWNER_ID, "daily_backup.json", json.dumps(dump, indent=2).encode(), "Daily auto-backup")
    supa.cleanup_old_updates()
    return jsonify({"ok": True, "transactions": len(dump["transactions"])})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "expense-bot"})
