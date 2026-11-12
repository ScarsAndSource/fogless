"""Run from the project root:   pytest -q      (or: python -m unittest discover -s tests -v)

Boots a fake Supabase (tests/fake_supabase.py), points the real Flask app at it, and drives the real
HTTP routes: sign-up / sign-in / refresh / logout / reset / delete, and -- the part that matters most --
that one user can never read, change or delete another user's data.
"""
import csv
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supa import FAKE, idx, g, User, PW, new_email, Base  # noqa: E402


# ============================================================================ authentication
class TestAuth(Base):
    def test_every_data_route_requires_login(self):
        c = idx.app.test_client()
        for path in ("/api/state", "/api/transactions", "/api/balances", "/api/aliases", "/api/history",
                     "/api/export", "/api/backup"):
            self.assertEqual(c.get(path).status_code, 401, path)
        for path in ("/api/message", "/api/action", "/api/voice", "/api/account/delete"):
            self.assertEqual(c.post(path, json={}).status_code, 401, path)

    def test_public_routes_need_no_login(self):
        c = idx.app.test_client()
        self.assertEqual(c.get("/api/config").status_code, 200)
        s = c.get("/api/session").json
        self.assertTrue(s["ok"])
        self.assertFalse(s["authenticated"])

    def test_signup_login_session_logout(self):
        email = new_email()
        c = idx.app.test_client()
        r = c.post("/api/signup", json={"email": email.upper(), "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json["signed_in"])
        self.assertEqual(c.get("/api/session").json["email"], email)  # normalised to lower case

        raw = r.headers.getlist("Set-Cookie")
        self.assertTrue(any(h.startswith("fl_at=") and "HttpOnly" in h and "SameSite=Lax" in h for h in raw), raw)
        self.assertTrue(any(h.startswith("fl_rt=") and "HttpOnly" in h for h in raw), raw)

        out = c.post("/api/logout")
        self.assertEqual(out.status_code, 200)
        cleared = [h for h in out.headers.getlist("Set-Cookie") if "Expires=Thu, 01 Jan 1970" in h]
        self.assertEqual(sorted(h.split("=")[0] for h in cleared), ["fl_at", "fl_rt"])  # both cookies removed
        self.assertFalse(c.get("/api/session").json["authenticated"])
        self.assertEqual(c.get("/api/state").status_code, 401)

        c2 = idx.app.test_client()
        r = c2.post("/api/login", json={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c2.get("/api/state").status_code, 200)

    def test_wrong_password_and_unknown_email_look_identical(self):
        u = User()
        c = idx.app.test_client()
        a = c.post("/api/login", json={"email": u.email, "password": "nope-nope-nope"})
        b = c.post("/api/login", json={"email": "ghost@example.com", "password": "nope-nope-nope"})
        self.assertEqual((a.status_code, a.json), (b.status_code, b.json))
        self.assertEqual(a.status_code, 401)
        self.assertEqual(c.post("/api/login", json={"email": "", "password": ""}).status_code, 400)

    def test_signup_validation(self):
        c = idx.app.test_client()
        self.assertEqual(c.post("/api/signup", json={"email": "not-an-email", "password": PW}).status_code, 400)
        self.assertEqual(c.post("/api/signup", json={"email": new_email(), "password": "short"}).status_code, 400)
        self.assertEqual(c.post("/api/signup", json={"email": new_email(), "password": "x" * 80}).status_code, 400)
        e = new_email()
        self.assertEqual(c.post("/api/signup", json={"email": e, "password": e}).status_code, 400)
        dup = User()
        r = idx.app.test_client().post("/api/signup", json={"email": dup.email, "password": PW})
        self.assertEqual(r.status_code, 409)

    def test_signup_closed_and_invite_code(self):
        c = idx.app.test_client()
        idx.SIGNUPS_OPEN = False
        self.assertEqual(c.post("/api/signup", json={"email": new_email(), "password": PW}).status_code, 403)
        idx.SIGNUPS_OPEN, idx.SIGNUP_CODE = True, "let-me-in"
        self.assertTrue(c.get("/api/config").json["needs_code"])
        self.assertEqual(c.post("/api/signup", json={"email": new_email(), "password": PW}).status_code, 403)
        self.assertEqual(c.post("/api/signup", json={"email": new_email(), "password": PW,
                                                     "code": "wrong"}).status_code, 403)
        r = c.post("/api/signup", json={"email": new_email(), "password": PW, "code": "let-me-in"})
        self.assertEqual(r.status_code, 200)

    def test_email_confirmation_flow(self):
        FAKE.confirm_email = True
        email = new_email()
        c = idx.app.test_client()
        r = c.post("/api/signup", json={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json["signed_in"])
        self.assertEqual(c.get("/api/state").status_code, 401)  # not signed in until confirmed
        r = c.post("/api/login", json={"email": email, "password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Confirm your email", r.json["text"])
        FAKE.confirm(email)
        self.assertEqual(c.post("/api/login", json={"email": email, "password": PW}).status_code, 200)
        # an existing address gets the same answer as a new one (no account enumeration)
        again = idx.app.test_client().post("/api/signup", json={"email": email, "password": PW})
        self.assertEqual((again.status_code, again.json["signed_in"]), (200, False))

    def test_expired_access_token_is_refreshed_transparently(self):
        u = User()
        old_at = next(t for t, (uid, _s) in FAKE.access.items() if uid == u.id)
        FAKE.expired.add(old_at)
        idx._TOKEN_CACHE.clear()
        r = u.c.get("/api/state")
        self.assertEqual(r.status_code, 200)
        new = [h for h in r.headers.getlist("Set-Cookie") if h.startswith("fl_at=")]
        self.assertTrue(new, "a fresh access cookie should be issued")
        self.assertNotIn(old_at, new[0])
        self.assertEqual(u.c.get("/api/state").status_code, 200)  # and the new pair keeps working

    def test_dead_refresh_token_logs_out_and_clears_cookies(self):
        u = User()
        FAKE.refresh.clear()
        for t in list(FAKE.access):
            FAKE.expired.add(t)
        idx._TOKEN_CACHE.clear()
        r = u.c.get("/api/state")
        self.assertEqual(r.status_code, 401)
        cleared = [h for h in r.headers.getlist("Set-Cookie") if "Expires=Thu, 01 Jan 1970" in h]
        self.assertEqual(len(cleared), 2, r.headers.getlist("Set-Cookie"))

    def test_forged_or_garbage_cookie_is_rejected(self):
        c = idx.app.test_client()
        c.set_cookie("fl_at", "garbage", path="/api/")
        self.assertEqual(c.get("/api/state").status_code, 401)
        c.set_cookie("fl_at", "at-" + "0" * 48, path="/api/")
        c.set_cookie("fl_rt", "rt-" + "0" * 48, path="/api/")
        self.assertEqual(c.get("/api/state").status_code, 401)

    def test_logout_revokes_the_session_server_side(self):
        u = User()
        at = next(t for t, (uid, _s) in FAKE.access.items() if uid == u.id)
        rt = next(t for t, (uid, _s) in FAKE.refresh.items() if uid == u.id)
        u.c.post("/api/logout")
        thief = idx.app.test_client()
        thief.set_cookie("fl_at", at, path="/api/")
        thief.set_cookie("fl_rt", rt, path="/api/")
        self.assertEqual(thief.get("/api/state").status_code, 401)

    def test_cross_site_post_is_blocked(self):
        u = User()
        r = u.c.post("/api/message", json={"text": "5 tea cash"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = u.c.post("/api/message", json={"text": "5 tea cash"}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(u.state()["history"]), 1)

    def test_password_reset_flow(self):
        u = User()
        anon = idx.app.test_client()
        a = anon.post("/api/forgot", json={"email": u.email})
        self.assertEqual(FAKE.recover_requests[-1], (u.email, "http://localhost/reset.html"))
        b = anon.post("/api/forgot", json={"email": "ghost@example.com"})
        self.assertEqual((a.status_code, a.json), (b.status_code, b.json))  # identical either way
        idx.SITE_URL = "https://fogless.example"
        try:
            anon.post("/api/forgot", json={"email": u.email})
            self.assertEqual(FAKE.recover_requests[-1][1], "https://fogless.example/reset.html")
        finally:
            idx.SITE_URL = ""

        token = FAKE.make_recovery_token(u.email)
        self.assertEqual(anon.post("/api/reset-password", json={"access_token": token, "password": "short"}).status_code, 400)
        self.assertEqual(anon.post("/api/reset-password", json={"access_token": token, "password": PW}).status_code, 400)  # same
        self.assertEqual(anon.post("/api/reset-password", json={"access_token": "bogus", "password": "brand-new-pass-1"}).status_code, 401)
        r = anon.post("/api/reset-password", json={"access_token": token, "password": "brand-new-pass-1"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(anon.post("/api/login", json={"email": u.email, "password": PW}).status_code, 401)
        self.assertEqual(anon.post("/api/login", json={"email": u.email, "password": "brand-new-pass-1"}).status_code, 200)

    def test_service_key_never_used_for_user_auth_and_anon_key_never_for_data(self):
        User()
        for method, path, role in FAKE.calls:
            if path.startswith("/rest/v1/"):
                self.assertEqual(role, "service", (method, path))
            elif path.startswith("/auth/v1/admin/"):
                self.assertEqual(role, "service", (method, path))
            else:
                self.assertEqual(role, "anon", (method, path))

    def test_auth_outage_is_a_503_not_a_leak(self):
        u = User()
        saved = idx.SUPABASE_URL
        idx.SUPABASE_URL = "http://127.0.0.1:9"  # nothing listens here
        idx._TOKEN_CACHE.clear()
        try:
            self.assertEqual(u.c.get("/api/state").status_code, 503)
        finally:
            idx.SUPABASE_URL = saved


# ============================================================================ data isolation
class TestIsolation(Base):
    def setUp(self):
        self.a, self.b = User(), User()
        self.assertEqual(self.a.msg("24 coffee upi").status_code, 200)
        self.assertEqual(self.a.msg("got 424 upi").status_code, 200)
        self.assertEqual(self.b.msg("50 bus cash").status_code, 200)

    def test_each_user_sees_only_their_own_ledger(self):
        sa, sb = self.a.state(), self.b.state()
        self.assertEqual([h["amount"] for h in sa["history"]], [424.0, 24.0])
        self.assertEqual([h["amount"] for h in sb["history"]], [50.0])
        self.assertAlmostEqual(sa["balance"], 400.0)
        self.assertAlmostEqual(sb["balance"], -50.0)
        a_ids = {t["id"] for t in self.a.c.get("/api/transactions").json}
        b_ids = {t["id"] for t in self.b.c.get("/api/transactions").json}
        self.assertTrue(a_ids and b_ids and not (a_ids & b_ids))

    def test_cannot_delete_or_edit_someone_elses_transaction(self):
        victim = self.a.history_ids()[0]
        r = self.b.c.post("/api/action", json={"action": "delete", "tx_id": victim})
        self.assertEqual(r.status_code, 404)
        r = self.b.c.post("/api/action", json={"action": "set_category", "tx_id": victim, "category": "other"})
        self.assertEqual(r.status_code, 404)
        r = self.b.c.post("/api/action", json={"action": "set_payment", "tx_id": victim, "payment_method": "cash"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(len(self.a.state()["history"]), 2)  # untouched
        r = self.a.c.post("/api/action", json={"action": "delete", "tx_id": victim})  # the owner still can
        self.assertEqual(r.status_code, 200)

    def test_undo_only_touches_own_data(self):
        self.b.c.post("/api/action", json={"action": "undo"})
        self.assertEqual(len(self.b.state()["history"]), 0)
        self.assertEqual(len(self.a.state()["history"]), 2)

    def test_starting_balances_are_per_user(self):
        self.a.msg("/balance 500 cash")
        self.assertAlmostEqual(self.a.balances()["balances"]["cash"], 500.0)
        self.assertAlmostEqual(self.b.balances()["balances"]["cash"], -50.0)
        self.b.msg("/balance 100 cash")
        self.assertAlmostEqual(self.a.balances()["balances"]["cash"], 500.0)  # not overwritten by B
        self.assertAlmostEqual(self.b.balances()["balances"]["cash"], 100.0)
        keys = [(r["user_id"], r["key"]) for r in FAKE.tables["settings"] if r["user_id"] in (self.a.id, self.b.id)]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual({u for u, _ in keys}, {self.a.id, self.b.id})

    def test_learned_aliases_are_per_user(self):
        tx = self.a.c.get("/api/transactions").json[1]  # the "coffee" expense
        r = self.a.c.post("/api/action", json={"action": "set_category", "tx_id": tx["id"], "category": "food"})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(self.a.c.get("/api/aliases").json)
        self.assertEqual(self.b.c.get("/api/aliases").json, [])
        self.assertTrue(FAKE.rows_for("aliases", self.a.id))
        self.assertNotIn("coffee", {r["note_key"] for r in FAKE.rows_for("aliases", self.b.id)})

    def test_chat_history_is_private(self):
        mine = [m["content"] for m in self.a.c.get("/api/history").json]
        theirs = [m["content"] for m in self.b.c.get("/api/history").json]
        self.assertTrue(any("coffee" in m for m in mine))
        self.assertFalse(any("coffee" in m for m in theirs))
        self.assertTrue(all("user_id" not in m for m in self.a.c.get("/api/history").json))

    def test_export_and_backup_contain_only_own_rows(self):
        rows = list(csv.DictReader(io.StringIO(self.b.c.get("/api/export").get_data(as_text=True))))
        self.assertEqual([r["amount"] for r in rows], ["50.0"] if rows[0]["amount"] == "50.0" else ["50"])
        dump = json.loads(self.b.c.get("/api/backup").get_data(as_text=True))
        self.assertEqual(len(dump["transactions"]), 1)
        self.assertTrue(all("user_id" not in t for t in dump["transactions"]))
        mine = json.loads(self.a.c.get("/api/backup").get_data(as_text=True))
        self.assertEqual(len(mine["transactions"]), 2)

    def test_reset_wipes_only_the_callers_data(self):
        before_b = {t: len(FAKE.rows_for(t, self.b.id)) for t in FAKE.tables}
        r = self.a.msg("/reset confirm")
        self.assertEqual(r.status_code, 200)
        self.assertIn("reset", r.json["text"].lower())
        self.assertEqual(self.a.state()["history"], [])
        for t in ("transactions", "aliases", "pool_contributions", "pool_expenses"):
            self.assertEqual(FAKE.rows_for(t, self.a.id), [], t)
        after_b = {t: len(FAKE.rows_for(t, self.b.id)) for t in FAKE.tables}
        self.assertEqual(before_b, after_b)
        self.assertEqual(len(self.b.state()["history"]), 1)

    def test_every_row_in_the_database_belongs_to_exactly_one_known_user(self):
        self.a.msg("/balance 10 upi")
        self.a.msg("120 lunch upi, 50 bus cash")
        for table, rows in FAKE.tables.items():
            for r in rows:
                self.assertIn(r["user_id"], FAKE.users, table)  # no orphans, no unowned rows

    def test_pools_and_debts_are_isolated(self):
        with idx.app.test_request_context():
            g.uid = self.a.id
            idx.add_pool_contribution("goa-trip", "ravi", 500)
            idx.add_pool_expense("goa-trip", 200, "travel")
            self.assertEqual(idx.list_known_pools(), ["goa-trip"])
            row = idx.get_pool_contributions()[0]
        with idx.app.test_request_context():
            g.uid = self.b.id
            self.assertEqual(idx.list_known_pools(), [])
            self.assertEqual(idx.get_pool_contributions(), [])
            self.assertFalse(idx.delete_pool_contribution(row["id"]))  # cannot delete A's row by id
            self.assertFalse(idx.delete_pool_expense(1))
        with idx.app.test_request_context():
            g.uid = self.a.id
            self.assertEqual(len(idx.get_pool_contributions()), 1)

    def test_rate_limit_is_per_user(self):
        idx.RATE_LIMIT_PER_MIN = 3
        c = User()
        codes = [c.msg(f"{i + 1} tea cash").status_code for i in range(5)]
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3:], [429, 429])
        self.assertEqual(self.b.msg("5 tea cash").status_code, 200)  # someone else's burst doesn't block B
        idx.RATE_LIMIT_PER_MIN, idx.RATE_LIMIT_PER_DAY = 100, 4
        d = User()
        self.assertEqual([d.msg("1 tea cash").status_code for _ in range(5)], [200, 200, 200, 200, 429])

    def test_timezone_is_per_user(self):
        nz, la = User(tz="Pacific/Auckland"), User(tz="America/Los_Angeles")
        self.assertEqual(FAKE.users[nz.id]["meta"]["timezone"], "Pacific/Auckland")
        nz.msg("10 tea cash")
        la.msg("10 tea cash")
        for u, zone in ((nz, "Pacific/Auckland"), (la, "America/Los_Angeles")):
            row = u.state()["history"][0]
            stamp = datetime.fromisoformat(row["created_at"]).astimezone(ZoneInfo(zone)).strftime("%d %b, %I:%M %p")
            self.assertEqual(row["created_at_display"], stamp)
        # an account with no stored timezone keeps the original IST behaviour
        legacy = User()
        legacy.msg("10 tea cash")
        row = legacy.state()["history"][0]
        self.assertEqual(row["created_at_display"],
                         datetime.fromisoformat(row["created_at"]).astimezone(idx.IST).strftime("%d %b, %I:%M %p"))

    def test_login_updates_the_stored_timezone(self):
        u = User(tz="Asia/Kolkata")
        c = idx.app.test_client()
        c.post("/api/login", json={"email": u.email, "password": PW, "tz": "Europe/London"})
        self.assertEqual(FAKE.users[u.id]["meta"]["timezone"], "Europe/London")
        c.post("/api/login", json={"email": u.email, "password": PW, "tz": "../../etc/passwd"})  # junk is ignored
        self.assertEqual(FAKE.users[u.id]["meta"]["timezone"], "Europe/London")

    def test_delete_account_removes_data_and_login(self):
        victim = User()
        victim.msg("77 snacks cash")
        self.assertTrue(FAKE.rows_for("transactions", victim.id))
        old_at = next(t for t, (uid, _s) in FAKE.access.items() if uid == victim.id)
        self.assertEqual(victim.c.post("/api/account/delete", json={"password": "wrong-wrong"}).status_code, 403)
        self.assertEqual(victim.c.post("/api/account/delete", json={}).status_code, 403)
        self.assertTrue(FAKE.rows_for("transactions", victim.id))  # still there
        r = victim.c.post("/api/account/delete", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertNotIn(victim.id, FAKE.users)
        for t in FAKE.tables:
            self.assertEqual(FAKE.rows_for(t, victim.id), [], t)
        self.assertEqual(victim.c.get("/api/state").status_code, 401)
        self.assertEqual(idx.app.test_client().post(
            "/api/login", json={"email": victim.email, "password": PW}).status_code, 401)
        self.assertNotIn(old_at, FAKE.access)
        self.assertEqual(len(self.a.state()["history"]), 2)  # everyone else untouched
        self.assertEqual(len(self.b.state()["history"]), 1)


# ============================================================================ the choke point itself
class TestScopingGuard(Base):
    def test_no_user_no_query(self):
        with idx.app.test_request_context():
            with self.assertRaises(idx.AuthError):
                idx._sb("GET", "transactions")
            with self.assertRaises(idx.AuthError):
                idx.get_balance()

    def test_unscoped_tables_are_refused(self):
        u = User()
        with idx.app.test_request_context():
            g.uid = u.id
            for table in ("message_log", "processed_updates", "users", "nope"):
                with self.assertRaises(idx.StorageError):
                    idx._sb("GET", table)

    def test_callers_cannot_override_the_user_filter_or_the_owner_of_written_rows(self):
        a, b = User(), User()
        with idx.app.test_request_context():
            g.uid = a.id
            idx._sb("POST", "chat_log", json_body={"role": "user", "content": "hi", "user_id": b.id},
                    prefer="return=minimal")
            r = idx._sb("GET", "chat_log", params={"select": "*", "user_id": f"eq.{b.id}"})
            self.assertEqual(len(idx._rows(r)), 1)  # the spoofed filter was replaced by A's own
            self.assertEqual(idx._rows(r)[0]["user_id"], a.id)
        self.assertEqual(FAKE.rows_for("chat_log", b.id), [])
        method, table, params, body = FAKE.rest_requests[-1]
        self.assertEqual(params["user_id"], f"eq.{a.id}")
        post = next(x for x in reversed(FAKE.rest_requests) if x[0] == "POST" and x[1] == "chat_log")
        self.assertEqual(post[3]["user_id"], a.id)  # the spoofed owner in the body was overwritten

    def test_every_data_request_carries_the_user_filter(self):
        u = User()
        FAKE.rest_requests.clear()
        u.msg("24 coffee upi")
        u.c.get("/api/state")
        u.c.get("/api/export")
        u.msg("/balance 100 cash")
        u.msg("/reset confirm")
        self.assertTrue(FAKE.rest_requests)
        for method, table, params, body in FAKE.rest_requests:
            if table.startswith("rpc/"):  # database functions take the owner as their only argument
                self.assertEqual(body, {"p_user_id": u.id}, (table, body))
            elif method == "POST":  # inserts have no row filter: every row written is stamped with the owner
                rows = body if isinstance(body, list) else [body]
                self.assertTrue(rows and all(r.get("user_id") == u.id for r in rows), (table, body))
            else:
                self.assertEqual(params.get("user_id"), f"eq.{u.id}", (method, table, params))


if __name__ == "__main__":
    unittest.main()
