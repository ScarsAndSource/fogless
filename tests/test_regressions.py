"""Regression tests for the production-readiness pass (key headers, message limits, LLM caps, refresh race)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supa import FAKE, idx, User, Base  # noqa: E402

JWT_LIKE = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9.sig"


class TestKeyHeaders(unittest.TestCase):
    def test_new_style_keys_travel_only_in_apikey(self):
        for key in ("sb_secret_abc123", "sb_publishable_abc123"):
            self.assertFalse(idx._is_jwt(key))
            h = idx._key_headers(key)
            self.assertEqual(h["apikey"], key)
            self.assertNotIn("Authorization", h)

    def test_legacy_jwt_keys_keep_the_bearer_header(self):
        self.assertTrue(idx._is_jwt(JWT_LIKE))
        h = idx._key_headers(JWT_LIKE)
        self.assertEqual(h["Authorization"], f"Bearer {JWT_LIKE}")

    def test_user_token_is_still_sent_as_bearer(self):
        u = User()  # sign-up/sign-in/refresh all work with a non-JWT key + user bearer tokens
        self.assertEqual(u.c.get("/api/session").json["email"], u.email)


class TestLimits(Base):
    def test_overlong_message_is_rejected_before_it_is_stored(self):
        u = User()
        r = u.msg("x" * (idx.MAX_TEXT_LEN + 1))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(u.c.get("/api/history").json, [])

    def test_llm_item_count_is_capped(self):
        n = idx.MAX_ITEMS_PER_MESSAGE + 5
        items = [{"type": "expense", "amount": i + 1, "category": "food"} for i in range(n)]

        class R:
            def raise_for_status(self): pass
            def json(self): return {"choices": [{"message": {"content": json.dumps({"transactions": items})}}]}

        u = User()
        old = idx.GROQ_API_KEY
        idx.GROQ_API_KEY = "x"
        try:
            with mock.patch.object(idx.requests, "post", return_value=R()):
                r = u.msg("100 a, 200 b")
        finally:
            idx.GROQ_API_KEY = old
        self.assertEqual(r.status_code, 200)
        self.assertIn("NOT logged", r.json["text"])
        self.assertEqual(len(u.c.get("/api/transactions?limit=1000").json), idx.MAX_ITEMS_PER_MESSAGE)


class TestRefreshRace(Base):
    def test_already_used_refresh_token_keeps_cookies(self):
        u = User()
        FAKE.expired.update(t for t in list(FAKE.access))
        with mock.patch.object(idx, "_auth_refresh",
                               return_value=(400, {"error_code": "refresh_token_already_used"})):
            r = u.c.get("/api/state")
        self.assertEqual(r.status_code, 401)
        self.assertFalse([h for h in r.headers.getlist("Set-Cookie") if "Expires=Thu, 01 Jan 1970" in h])


if __name__ == "__main__":
    unittest.main()
