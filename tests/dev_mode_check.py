"""Executed in a fresh process by test_dev_mode.py (module-level config is read at import time).
Usage: dev_mode_check.py <scenario>   scenarios: local | vercel | unconfigured"""
import contextlib
import io
import os
import re
import sys

scenario = sys.argv[1]
os.environ["FOGLESS_NO_DOTENV"] = "1"  # never read a real .env in tests
for var in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY", "VERCEL",
            "FOGLESS_DEV_MODE", "GROQ_API_KEY"):
    os.environ.pop(var, None)
if scenario in ("local", "vercel"):
    os.environ["FOGLESS_DEV_MODE"] = "1"
if scenario == "vercel":
    os.environ["VERCEL"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import api.index as idx  # noqa: E402

PW = "correct-horse-battery"

if scenario in ("vercel", "unconfigured"):
    # Fail closed: no in-memory accounts unless explicitly local, and never on Vercel.
    assert not idx._dev_auth()
    r = idx.app.test_client().post("/api/signup", json={"email": "a@example.com", "password": PW})
    assert r.status_code == 503, (r.status_code, r.json)
    assert idx.app.test_client().get("/api/state").status_code == 401
    print("ok", scenario)
    sys.exit(0)

assert idx._dev_auth() and not idx._use_sb()


def user(email):
    c = idx.app.test_client()
    r = c.post("/api/signup", json={"email": email, "password": PW, "tz": "Asia/Kolkata"})
    assert r.status_code == 200 and r.json["signed_in"], r.json
    return c


a, b = user("a@example.com"), user("b@example.com")
assert a.post("/api/message", json={"text": "24 coffee upi"}).status_code == 200
assert a.post("/api/message", json={"text": "got 424 upi"}).status_code == 200
assert b.post("/api/message", json={"text": "50 bus cash"}).status_code == 200
sa, sb = a.get("/api/state").json, b.get("/api/state").json
assert [h["amount"] for h in sa["history"]] == [424.0, 24.0], sa["history"]
assert [h["amount"] for h in sb["history"]] == [50.0], sb["history"]

victim = a.get("/api/state").json["history"][0]["id"]
assert b.post("/api/action", json={"action": "delete", "tx_id": victim}).status_code == 404
a.post("/api/message", json={"text": "/balance 500 cash"})
assert round(a.get("/api/balances").json["balances"]["cash"]) == 500
assert round(b.get("/api/balances").json["balances"]["cash"]) == -50
assert b.get("/api/aliases").json == []

assert a.post("/api/message", json={"text": "/reset confirm"}).status_code == 200
assert a.get("/api/state").json["history"] == []
assert len(b.get("/api/state").json["history"]) == 1  # reset did not touch B

# password reset: the dev "email" is a link printed to stdout
anon = idx.app.test_client()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    assert anon.post("/api/forgot", json={"email": "b@example.com"}).status_code == 200
token = re.search(r"access_token=([\w\-]+)&type=recovery", buf.getvalue()).group(1)
assert anon.post("/api/reset-password", json={"access_token": token, "password": "another-new-pass"}).status_code == 200
assert anon.post("/api/login", json={"email": "b@example.com", "password": PW}).status_code == 401
assert anon.post("/api/login", json={"email": "b@example.com", "password": "another-new-pass"}).status_code == 200

# refresh-token rotation + logout
c = idx.app.test_client()
c.post("/api/login", json={"email": "a@example.com", "password": PW})
at = c.get_cookie("fl_at", path="/api/").value
idx._DEV_TOKENS.pop(at)  # simulate expiry of just the access token
idx._TOKEN_CACHE.clear()
assert c.get("/api/state").status_code == 200  # refreshed transparently
assert c.get_cookie("fl_at", path="/api/").value != at
c.post("/api/logout")
assert c.get("/api/state").status_code == 401

# account deletion
assert b.post("/api/account/delete", json={"password": "wrong-pass-123"}).status_code == 403
assert b.post("/api/account/delete", json={"password": "another-new-pass"}).status_code == 200
assert "b@example.com" not in idx._DEV_USERS
assert b.get("/api/state").status_code == 401
assert a.get("/api/state").status_code == 200  # A untouched
print("ok local")
