"""Shared bootstrap for every test that talks to the fake Supabase. The app reads its configuration at import
time, so it is imported exactly once, here, pointed at the fake."""
import itertools
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from fake_supabase import FakeSupabase, ANON_KEY, SERVICE_KEY  # noqa: E402

FAKE = FakeSupabase()
os.environ.update(SUPABASE_URL=FAKE.url, SUPABASE_SERVICE_KEY=SERVICE_KEY, SUPABASE_ANON_KEY=ANON_KEY,
                  GROQ_API_KEY="", FOGLESS_NO_DOTENV="1")  # no Groq key -> local rule-based parser; no .env -> never real keys
for var in ("VERCEL", "FOGLESS_DEV_MODE", "SIGNUP_CODE", "SIGNUPS_OPEN", "SITE_URL"):
    os.environ.pop(var, None)
sys.path.insert(0, ROOT)
import api.index as idx  # noqa: E402
from flask import g  # noqa: E402

PW = "correct-horse-battery"
_n = itertools.count(1)


def new_email():
    return f"user{next(_n)}@example.com"


class User:
    """A signed-up browser: its own cookie jar, its own account."""

    def __init__(self, tz=None):
        self.c = idx.app.test_client()
        self.email = new_email()
        r = self.c.post("/api/signup", json={"email": self.email, "password": PW, "tz": tz})
        assert r.status_code == 200 and r.json["signed_in"], r.json
        self.id = FAKE.user_by_email(self.email)["id"]

    def msg(self, text):
        return self.c.post("/api/message", json={"text": text})

    def state(self):
        return self.c.get("/api/state").json

    def balances(self):
        return self.c.get("/api/balances").json

    def history_ids(self):
        return [h["id"] for h in self.state()["history"]]


class Base(unittest.TestCase):
    def setUp(self):
        idx._THROTTLE.clear()  # every test client shares 127.0.0.1; the per-IP abuse limits must start empty

    def tearDown(self):
        idx._THROTTLE.clear()
        FAKE.confirm_email = False
        idx.SIGNUPS_OPEN, idx.SIGNUP_CODE = True, ""
        idx.RATE_LIMIT_PER_MIN, idx.RATE_LIMIT_PER_DAY = 20, 300
        FAKE.rpc_installed, FAKE.fail_bulk_transactions = True, False
        idx._RPC["ok"] = None
        idx.SB_PAGE_SIZE = 1000


