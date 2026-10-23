"""
final_patch.py  --  run ONCE from the project root:   python final_patch.py

What it does to api/index.py:
  1. removes every piece of the Telegram bot (webhook, sender, update de-dupe, edit tracking, env vars)
  2. adds the demo login (vaibhav10 / iwon10): /api/login, /api/logout, /api/session, and a
     guard that makes every other /api/* route require the signed session cookie
  3. hardens the local static-file route against ../ path escapes

All-or-nothing: every replacement is checked first, the result is compile-checked, and the file is
only written if everything passed. If anything doesn't match, NOTHING is changed.
Line endings (CRLF/LF) are preserved. Undo with:  git checkout api/index.py
"""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "api/index.py"

raw = open(PATH, "rb").read().decode("utf-8")
crlf = "\r\n" in raw
src = raw.replace("\r\n", "\n")

if "def api_login" in src:
    sys.exit("This file already has the demo login - patch already applied. Nothing changed.")


def rep(old, new, count=1):
    global src
    n = src.count(old)
    if n != count:
        sys.exit(f"PATCH FAILED: expected {count} match(es), found {n} for:\n{old[:160]}\nNothing was changed.")
    src = src.replace(old, new)


def cut(start_marker, end_marker):
    """Delete everything from start_marker up to (not including) end_marker."""
    global src
    if src.count(start_marker) != 1 or src.count(end_marker) != 1:
        sys.exit(f"PATCH FAILED: markers not found exactly once:\n{start_marker[:100]}\n{end_marker[:100]}\nNothing was changed.")
    a, b = src.index(start_marker), src.index(end_marker)
    if a >= b:
        sys.exit("PATCH FAILED: markers out of order. Nothing was changed.")
    src = src[:a] + src[b:]


# ------------------------------------------------------------------ imports
rep("""from flask import Flask, request, jsonify, send_file, Response, render_template_string
import threading
import traceback
from html import escape as _esc, unescape as _unesc
from werkzeug.exceptions import HTTPException
""", """from flask import Flask, request, jsonify, send_file, Response, render_template_string, session
import hashlib
import hmac
import time
import traceback
from html import escape as _esc, unescape as _unesc
from werkzeug.exceptions import HTTPException
from werkzeug.utils import safe_join
""")

# ------------------------------------------------------------------ config
rep('''BOT_TOKEN = os.getenv("BOT_TOKEN", "")
try:
    OWNER_ID = int((os.getenv("OWNER_ID", "0") or "0").strip())
except ValueError:
    # A garbage OWNER_ID must lock the bot (nobody matches -1), never crash the import or open it up.
    print("[config] OWNER_ID is not a number - bot locked until it is fixed")
    OWNER_ID = -1
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "")
CRON_SECRET = os.getenv("CRON_SECRET", "")
''', '''CRON_SECRET = os.getenv("CRON_SECRET", "")

# Demo login. Override with DEMO_USERNAME / DEMO_PASSWORD env vars if you ever want different ones.
DEMO_USERNAME = os.getenv("DEMO_USERNAME", "vaibhav10")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "iwon10")
''')

rep('''TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""


''', '')

rep('''SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))
''', '''SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", os.getenv("SUPABASE_KEY", ""))

# Signs the login cookie. Set SESSION_SECRET in production; without it we derive a stable secret
# from the Supabase key (so every serverless instance agrees), or a fixed one for local testing.
SESSION_SECRET = os.getenv("SESSION_SECRET") or (
    hashlib.sha256(("fogless-session:" + SUPABASE_KEY).encode("utf-8")).hexdigest()
    if SUPABASE_KEY else "fogless-local-demo-secret"
)
''')

rep('''app = Flask(__name__)
''', '''app = Flask(__name__)
app.secret_key = SESSION_SECRET
app.config.update(
    SESSION_COOKIE_NAME="fogless_session",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.getenv("VERCEL")),  # https on Vercel; plain http works locally
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)
''')

# ------------------------------------------------------------------ telegram sender
cut("def send_telegram_message(",
    "# ---------------------------------------------------------------- data layer")

# ------------------------------------------------------------------ per-request collector + telegram-only stores
rep('''# In-memory ids / stores that only exist for the no-Supabase (local testing) mode
_mem_pool_id_counter = 1
_mem_processed = set()
_mem_msglog = {}

# Per-request collector so the Telegram handler can remember which rows a message created
_tl = threading.local()


def _track(kind, row_id):
    created = getattr(_tl, "created", None)
    if created is not None and row_id is not None:
        created[kind].append(row_id)


class _Collect:
    """with _Collect() as made: ...  ->  made = {"tx": [...], "pc": [...], "pe": [...]}"""

    def __enter__(self):
        self.made = {"tx": [], "pc": [], "pe": []}
        _tl.created = self.made
        return self.made

    def __exit__(self, *exc):
        _tl.created = None
        return False
''', '''# In-memory id counter that only exists for the no-Supabase (local testing) mode
_mem_pool_id_counter = 1
''')

rep('        _track("tx", tx_id)\n        return tx_id\n', '        return tx_id\n', count=2)
rep('    _track("tx", tx_id)\n    return tx_id\n', '    return tx_id\n', count=2)
rep('        _track("pc", row_id)\n        return row_id\n', '        return row_id\n')
rep('    _track("pc", row_id)\n    return row_id\n', '    return row_id\n')
rep('        _track("pe", row_id)\n        return row_id\n', '        return row_id\n')
rep('    _track("pe", row_id)\n    return row_id\n', '    return row_id\n')

# ------------------------------------------------------------------ idempotency + message-log block
cut("# --------------------------------------------------------- telegram idempotency + edit tracking",
    "def reset_all_data()")

rep('''    """Really wipes the ledger (Supabase tables too when configured). processed_updates is kept so
    old Telegram deliveries can never be replayed after a reset."""''',
    '''    """Really wipes the ledger (Supabase tables too when configured)."""''')
rep('        _sb("DELETE", "message_log", params={"chat_id": "neq.0"})\n', '')
rep('    _mem_msglog.clear()\n', '')

# ------------------------------------------------------------------ static files: no webhook case, safe join
rep('''@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    if not filename.startswith("api/") and not filename.startswith("webhook"):
        file_path = os.path.join(FRONTEND_DIR, filename)
        if os.path.exists(file_path):
            return send_file(file_path)
    return jsonify({"ok": False, "error": "Not Found"}), 404
''', '''@app.route("/login", methods=["GET"])
def login_page():
    return send_file(os.path.join(FRONTEND_DIR, "login.html"))


@app.route("/<path:filename>", methods=["GET"])
def static_files(filename):
    if not filename.startswith("api/"):
        file_path = safe_join(FRONTEND_DIR, filename)  # refuses ../ escapes
        if file_path and os.path.isfile(file_path):
            return send_file(file_path)
    return jsonify({"ok": False, "error": "Not Found"}), 404
''')

# ------------------------------------------------------------------ the demo login itself
AUTH_BLOCK = '''# ---------------------------------------------------------------------- Demo login (cookie session)

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


'''
rep('''@app.errorhandler(StorageError)
def _handle_storage_error(e):''', AUTH_BLOCK + '''@app.errorhandler(StorageError)
def _handle_storage_error(e):''')

# ------------------------------------------------------------------ webhook handler (to just before __main__)
cut("# --------------------------------------------------------------------- Telegram Webhook Handler",
    'if __name__ == "__main__":')

# ------------------------------------------------------------------ clean up leftover docstring mentions
rep('    """Plain-text version of one of our HTML cards (Telegram cannot render HTML)."""\n',
    '    """Plain-text version of one of our HTML cards."""\n')

# ------------------------------------------------------------------ final safety checks, then write
# Only check for FUNCTIONAL references (imports, config vars, function calls) - not docstrings/comments
for bad in ("BOT_TOKEN", "OWNER_ID", "TG_API", "claim_update",
            "_mem_msglog", "_mem_processed", "_Collect", "_track(", "threading",
            "def send_telegram", "def webhook(", "def _handle_telegram"):
    if bad in src:
        sys.exit(f"PATCH FAILED: leftover reference to '{bad}' after patching. Nothing was changed.")
try:
    compile(src, PATH, "exec")
except SyntaxError as e:
    sys.exit(f"PATCH FAILED: result would not compile ({e}). Nothing was changed.")

out = src.replace("\n", "\r\n") if crlf else src
with open(PATH, "wb") as f:
    f.write(out.encode("utf-8"))
print(f"OK - {PATH} patched ({'CRLF' if crlf else 'LF'} kept). Telegram removed, demo login added.")
