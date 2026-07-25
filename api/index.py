"""
Personal expense-tracker bot — serverless webhook version.
Runs as a Vercel Python function. No persistent process, no polling loop —
Telegram POSTs each update straight to /webhook, and the function replies
and exits. There's nothing to sleep and nothing to crash and stay down.

Commands (same as before):
  /add <amount> <category> [note...]
  /income <amount> <source> [note...]
  /balance
  /stats [today|week|month|all]
  /history [n]
  /undo
  /delete <id>
  /setbalance <amount>
  /export     -> CSV file
  /backup     -> full JSON dump
  /reset confirm
  /help
"""
import os
import io
import csv
import json
import requests
from flask import Flask, request, jsonify

import supa

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
TELEGRAM_SECRET_TOKEN = os.environ["TELEGRAM_SECRET_TOKEN"]  # set on setWebhook, verified per-request
CRON_SECRET = os.environ["CRON_SECRET"]  # separate secret for the GitHub Actions keepalive/backup ping

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)


def fmt(amount: float) -> str:
    return f"{amount:,.2f}"


def send_message(chat_id: int, text: str):
    requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=8)


def send_document(chat_id: int, filename: str, content_bytes: bytes, caption: str = None):
    files = {"document": (filename, content_bytes)}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    requests.post(f"{TG_API}/sendDocument", data=data, files=files, timeout=15)


HELP_TEXT = (
    "/add <amount> <category> [note] - log an expense\n"
    "/income <amount> <source> [note] - log income\n"
    "/balance - current balance\n"
    "/stats [today|week|month|all] - totals + breakdown\n"
    "/history [n] - last n transactions\n"
    "/undo - remove the most recent transaction\n"
    "/delete <id> - remove a specific transaction\n"
    "/setbalance <amount> - set your starting balance\n"
    "/export - get a CSV of everything\n"
    "/backup - get a full JSON backup\n"
    "/reset confirm - wipe all data\n"
)


def handle_command(chat_id: int, text: str):
    parts = text.strip().split()
    cmd = parts[0].lower().split("@")[0]  # strip @botname if present
    args = parts[1:]

    if cmd in ("/start", "/help"):
        send_message(chat_id, "Bot's live.\n\n" + HELP_TEXT)

    elif cmd == "/add":
        if len(args) < 2:
            return send_message(chat_id, "Usage: /add <amount> <category> [note]")
        try:
            amount = float(args[0])
        except ValueError:
            return send_message(chat_id, "Amount has to be a number.")
        category = args[1].lower()
        note = " ".join(args[2:]) if len(args) > 2 else None
        tx_id = supa.add_transaction("expense", amount, category, note)
        send_message(chat_id, f"Logged #{tx_id}: -{fmt(amount)} on {category}\nBalance: {fmt(supa.get_balance())}")

    elif cmd == "/income":
        if len(args) < 2:
            return send_message(chat_id, "Usage: /income <amount> <source> [note]")
        try:
            amount = float(args[0])
        except ValueError:
            return send_message(chat_id, "Amount has to be a number.")
        source = args[1].lower()
        note = " ".join(args[2:]) if len(args) > 2 else None
        tx_id = supa.add_transaction("income", amount, source, note)
        send_message(chat_id, f"Logged #{tx_id}: +{fmt(amount)} from {source}\nBalance: {fmt(supa.get_balance())}")

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
            lines.append(f"#{r['id']} {ts} {sign}{fmt(r['amount'])} {r['category']}{note}")
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
        writer.writerow(["id", "type", "amount", "category", "note", "created_at"])
        for r in rows:
            writer.writerow([r["id"], r["type"], r["amount"], r["category"], r.get("note"), r["created_at"]])
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


@app.route("/webhook", methods=["POST"])
def webhook():
    # verify this really came from Telegram, not a random POST to a guessed URL
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_SECRET_TOKEN:
        return jsonify({"ok": False}), 401

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")
    if not message:
        return jsonify({"ok": True})  # ignore non-message updates (reactions, etc.)

    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id")
    text = message.get("text", "")

    if user_id != OWNER_ID:
        # silently ignore anyone who isn't you — don't even acknowledge the bot exists
        return jsonify({"ok": True})

    if not text.startswith("/"):
        return jsonify({"ok": True})

    try:
        handle_command(chat_id, text)
    except Exception as e:
        # never let an unhandled error just vanish silently — but also never 500 to Telegram,
        # or it'll keep retrying the same failing update
        send_message(chat_id, f"Something went wrong on that one: {e}")

    return jsonify({"ok": True})


@app.route("/cron/backup", methods=["GET"])
def cron_backup():
    """
    Hit once a day by a GitHub Actions workflow. Does two jobs at once:
    1. Touches the Supabase DB (a real query) so the free project never hits its
       7-day inactivity pause.
    2. Sends a fresh JSON backup to your own Telegram chat as a safety net.
    """
    if request.args.get("token") != CRON_SECRET:
        return jsonify({"ok": False}), 401

    dump = supa.all_data_dump()
    send_document(OWNER_ID, "daily_backup.json", json.dumps(dump, indent=2).encode(), "Daily auto-backup")
    return jsonify({"ok": True, "transactions": len(dump["transactions"])})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "expense-bot"})
