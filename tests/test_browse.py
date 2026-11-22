"""Today's inflow, date-range browsing (/stats, /history, /api/browse) and chat-history cursors."""
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supa import FAKE, idx, User, Base  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")


def seed_tx(u, days_ago, typ, amount, category, note=None, pm="upi", hh=12, mm=0, tz=IST):
    """Insert a ledger row dated `days_ago` days back (local to tz), bypassing the parser."""
    day = (datetime.now(tz) - timedelta(days=days_ago)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    FAKE.seq["transactions"] += 1
    FAKE.tables["transactions"].append({
        "id": FAKE.seq["transactions"], "user_id": u.id, "type": typ, "amount": str(amount), "category": category,
        "note": note, "payment_method": pm, "created_at": day.astimezone(timezone.utc).isoformat()})


def browse(u, view="stats", rng=None, limit=None):
    q = f"/api/browse?view={view}" + (f"&range={rng}" if rng else "") + (f"&limit={limit}" if limit else "")
    return u.c.get(q)


def tile(html, label):
    m = re.search(rf'<span class="fl-k">{label}</span><span class="fl-v [^"]*">([^<]*)</span>', html)
    return m.group(1) if m else None


class TestTodayInflow(Base):
    def test_state_and_every_reply_carry_today_income(self):
        u = User(tz="Asia/Kolkata")
        self.assertEqual(u.state()["today_income"], 0.0)
        r = u.msg("got 500 upi").json
        self.assertEqual((r["today_income"], r["today_spent"]), (500.0, 0.0))
        r = u.msg("120 lunch cash").json
        self.assertEqual((r["today_income"], r["today_spent"]), (500.0, 120.0))
        s = u.state()
        self.assertEqual((s["today_income"], s["today_spent"]), (500.0, 120.0))

    def test_transfers_and_older_days_are_not_inflow(self):
        u = User(tz="Asia/Kolkata")
        seed_tx(u, 1, "income", 999, "salary")
        u.msg("gave rahul 200 cash")
        s = u.state()
        self.assertEqual((s["today_income"], s["today_spent"]), (0.0, 0.0))

    def test_inflow_is_private(self):
        a, b = User(tz="Asia/Kolkata"), User(tz="Asia/Kolkata")
        a.msg("got 700 upi")
        self.assertEqual(b.state()["today_income"], 0.0)


class TestRangeGrammar(Base):
    def setUp(self):
        super().setUp()
        self.u = User(tz="Asia/Kolkata")

    def rng(self, text):
        out = {}
        from api.index import _parse_range  # noqa: WPS433
        with idx.app.test_request_context():
            idx.g.tz = "Asia/Kolkata"
            out = _parse_range(text)
        return out

    def test_known_phrases(self):
        today = datetime.now(IST).date()
        self.assertEqual(self.rng("today")["start"], today)
        self.assertEqual(self.rng("yesterday")["start"], today - timedelta(days=1))
        self.assertEqual(self.rng("")["token"], "month")
        self.assertEqual(self.rng("all")["since"], None)
        self.assertEqual(self.rng("this month")["token"], "thismonth")
        self.assertEqual(self.rng("2026-11-20")["start"].isoformat(), "2026-11-20")
        self.assertEqual(self.rng("20/11/2026")["start"].isoformat(), "2026-11-20")  # day first
        self.assertEqual(self.rng("2025-02")["title"], "February 2025")
        self.assertEqual(self.rng("14 nov 2025")["start"].isoformat(), "2025-11-14")
        self.assertEqual(self.rng("nov 14 2025")["start"].isoformat(), "2025-11-14")

    def test_month_names_never_point_into_the_future(self):
        today = datetime.now(IST).date()
        for name in ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"):
            self.assertLessEqual(self.rng(name)["start"], today)

    def test_stepping_tokens(self):
        r = self.rng("2026-03")
        self.assertEqual((r["prev"], r["next"]), ("2026-02", "2026-04"))
        self.assertEqual(self.rng("2026-01")["prev"], "2025-12")
        d = self.rng("2026-03-01")
        self.assertEqual((d["prev"], d["next"]), ("2026-02-28", "2026-03-02"))
        self.assertIsNone(self.rng("today")["next"])        # you cannot step into the future
        self.assertEqual(self.rng("yesterday")["next"], "today")
        self.assertIsNone(self.rng("thismonth")["next"])

    def test_day_boundaries_are_the_users_midnight(self):
        r = self.rng("2026-11-20")  # IST = UTC+5:30 -> the day starts 18:30 UTC the evening before
        self.assertEqual(r["since"], "2026-11-19T18:30:00+00:00")
        self.assertEqual(r["until"], "2026-11-20T18:30:00+00:00")

    def test_garbage_is_rejected_not_guessed(self):
        for bad in ("blah", "2026-13", "2026-02-31", "99/99/2026", "1850-01", "2300-01-01", "nov 40"):
            with self.assertRaises(idx.RangeError, msg=bad):
                self.rng(bad)


class TestStatsCard(Base):
    def setUp(self):
        super().setUp()
        self.u = User(tz="Asia/Kolkata")
        seed_tx(self.u, 1, "expense", 180, "food", "thali", "upi")
        seed_tx(self.u, 1, "expense", 90, "transport", "auto", "cash", hh=20)
        seed_tx(self.u, 2, "income", 2500, "freelance", "logo", "upi")
        seed_tx(self.u, 40, "expense", 777, "bills", "wifi", "card")

    def test_yesterday_totals_and_extras(self):
        h = browse(self.u, "stats", "yesterday").json["html"]
        self.assertEqual((tile(h, "IN"), tile(h, "OUT"), tile(h, "NET")), ("₹0.00", "₹270.00", "-₹270.00"))
        for needle in ("Where it went", "Paid with", "Biggest spend", "Yesterday spend"):
            self.assertIn(needle, h)

    def test_day_with_income_shows_money_in(self):
        h = browse(self.u, "stats", (datetime.now(IST) - timedelta(days=2)).date().isoformat()).json["html"]
        self.assertEqual((tile(h, "IN"), tile(h, "OUT")), ("₹2,500.00", "₹0.00"))
        self.assertIn("Money in", h)

    def test_all_time_includes_the_old_row(self):
        h = browse(self.u, "stats", "all").json["html"]
        self.assertEqual(tile(h, "OUT"), "₹1,047.00")
        self.assertIn("All time", h)

    def test_empty_period_is_a_friendly_card_with_navigation(self):
        h = browse(self.u, "stats", "2020-01").json["html"]
        self.assertIn("Nothing logged", h)
        self.assertIn("fl-nav", h)

    def test_old_period_keywords_still_work_as_commands(self):
        for p in ("today", "week", "month", "all"):
            r = self.u.msg(f"/stats {p}")
            self.assertEqual(r.status_code, 200, p)
            self.assertIn("fl-browse", r.json["html"])
        self.assertEqual(self.u.msg("/stats").status_code, 200)

    def test_command_with_a_date_and_a_bad_one(self):
        self.assertEqual(self.u.msg("/stats nov 2025").status_code, 200)
        r = self.u.msg("/stats whenever")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Usage: /stats", r.json["text"])

    def test_chat_keeps_the_card(self):
        self.u.msg("/stats yesterday")
        logged = [m["content"] for m in self.u.c.get("/api/history").json if m["role"] == "assistant"]
        self.assertTrue(any("fl-browse" in c for c in logged))


class TestHistoryCard(Base):
    def setUp(self):
        super().setUp()
        self.u = User(tz="Asia/Kolkata")
        for i in range(29, -1, -1):  # oldest first, like a real ledger (ids grow with time)
            seed_tx(self.u, 1 + i // 3, "expense", 10 + i, "food", f"item {i}", "upi", hh=8 + i % 3)

    def rows(self, html):
        return len(re.findall(r'class="fl-row"', html))

    def test_default_is_twelve_with_date_time_and_more(self):
        h = self.u.msg("/history").json["html"]
        self.assertEqual(self.rows(h), 12)
        self.assertRegex(h, r"\d{1,2} \w{3} · \d{2}:\d{2} [AP]M")
        self.assertIn("Show 12 more", h)

    def test_show_more_grows_by_twelve_and_stops(self):
        self.assertEqual(self.rows(browse(self.u, "history", "all", 24).json["html"]), 24)
        last = browse(self.u, "history", "all", 36).json["html"]
        self.assertEqual(self.rows(last), 30)
        self.assertNotIn("fl-more", last)

    def test_old_count_syntax_and_day_syntax(self):
        self.assertEqual(self.rows(self.u.msg("/history 5").json["html"]), 5)
        self.assertEqual(self.rows(self.u.msg("/history 7 yesterday").json["html"]), 3)
        self.assertIn("Showing 3 of 3", self.u.msg("/history yesterday").json["html"])

    def test_a_day_number_is_a_date_not_a_count(self):
        self.assertIn("14 Nov", self.u.msg("/history 14 nov").json["html"])

    def test_range_view_has_totals_and_day_sums(self):
        h = browse(self.u, "history", "month").json["html"]
        self.assertIn("fl-day-sum", h)
        self.assertIsNotNone(tile(h, "OUT"))

    def test_newest_first_and_day_groups(self):
        h = browse(self.u, "history", "all", 12).json["html"]
        days = re.findall(r'<div class="fl-day"><span>([^<]*)</span>', h)
        self.assertTrue(days[0].startswith("Yesterday"), days)

    def test_range_views_order_by_date_even_if_ids_are_scrambled(self):
        other = User(tz="Asia/Kolkata")
        seed_tx(other, 0, "expense", 1, "food", "newest-by-date")   # lower id ...
        seed_tx(other, 9, "expense", 2, "food", "older-by-date")    # ... higher id, but an older date
        h = browse(other, "history", "month").json["html"]
        self.assertLess(h.index("newest-by-date"), h.index("older-by-date"))

    def test_hostile_notes_are_inert(self):
        seed_tx(self.u, 0, "expense", 5, "food", "<img src=x onerror=alert(1)>", "upi")
        h = browse(self.u, "history").json["html"]
        self.assertNotIn("<img", h)
        self.assertNotRegex(h, r"<[^>]*\son\w+\s*=")

    def test_limit_is_clamped(self):
        self.assertEqual(self.rows(browse(self.u, "history", "all", 99999).json["html"]), 30)
        self.assertEqual(browse(self.u, "history", "all", 0).status_code, 200)


class TestBrowseEndpoint(Base):
    def test_requires_sign_in(self):
        self.assertEqual(idx.app.test_client().get("/api/browse?view=stats").status_code, 401)

    def test_bad_input_is_a_400(self):
        u = User(tz="Asia/Kolkata")
        self.assertEqual(browse(u, "pie").status_code, 400)
        self.assertEqual(browse(u, "stats", "someday").status_code, 400)

    def test_never_writes_to_the_chat(self):
        u = User(tz="Asia/Kolkata")
        browse(u, "stats", "month")
        browse(u, "history", "all")
        self.assertEqual(u.c.get("/api/history").json, [])

    def test_each_user_only_sees_their_own_rows(self):
        a, b = User(tz="Asia/Kolkata"), User(tz="Asia/Kolkata")
        seed_tx(a, 1, "expense", 4242, "food", "secret-meal")
        self.assertIn("secret-meal", browse(a, "history", "all").json["html"])
        hb = browse(b, "history", "all").json["html"]
        self.assertNotIn("secret-meal", hb)
        self.assertNotIn("4,242", browse(b, "stats", "all").json["html"])

    def test_a_far_away_timezone_still_picks_the_right_local_day(self):
        u = User(tz="America/Los_Angeles")
        la = ZoneInfo("America/Los_Angeles")
        seed_tx(u, 1, "expense", 55, "food", "la-lunch", tz=la, hh=23, mm=30)   # 23:30 LA time yesterday
        yday = (datetime.now(la) - timedelta(days=1)).date().isoformat()
        self.assertIn("la-lunch", browse(u, "history", yday).json["html"])
        self.assertNotIn("la-lunch", browse(u, "history", "today").json["html"])


class TestChatCursors(Base):
    def setUp(self):
        super().setUp()
        idx.RATE_LIMIT_PER_MIN = 0   # this class sends dozens of messages in a second; Base.tearDown restores it
        self.u = User(tz="Asia/Kolkata")
        for i in range(25):
            self.u.msg(f"{i + 1} thing{i}")

    def ids(self, q):
        return [m["id"] for m in self.u.c.get("/api/history" + q).json]

    def test_default_is_newest_oldest_first(self):
        ids = self.ids("?limit=10")
        self.assertEqual(len(ids), 10)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(ids[-1], max(self.ids("?limit=500")))

    def test_before_pages_back_without_overlap(self):
        newest = self.ids("?limit=10")
        older = self.ids(f"?limit=10&before={newest[0]}")
        self.assertEqual(len(older), 10)
        self.assertLess(older[-1], newest[0])
        every = self.ids("?limit=500")
        self.assertEqual(older + newest, every[-20:])

    def test_after_returns_only_newer_rows(self):
        mark = max(self.ids("?limit=500"))
        self.assertEqual(self.ids(f"?after={mark}"), [])
        r = self.u.msg("77 late")
        new = self.ids(f"?after={mark}")
        self.assertEqual(len(new), 2)
        self.assertEqual(new, sorted(new))
        self.assertEqual(new[-1], r.json["chat_id"])

    def test_after_zero_means_everything_oldest_first(self):
        every = self.ids("?after=0&limit=500")
        self.assertEqual(every, sorted(every))
        self.assertEqual(len(every), 50)

    def test_reply_reports_the_newest_chat_id(self):
        r = self.u.msg("12 tea upi")
        self.assertEqual(r.json["chat_id"], max(self.ids("?limit=500")))

    def test_cursors_never_cross_users(self):
        other = User(tz="Asia/Kolkata")
        other.msg("99 theirs")
        mine = {m["id"] for m in self.u.c.get("/api/history?after=0&limit=500").json}
        theirs = {m["id"] for m in other.c.get("/api/history?after=0&limit=500").json}
        self.assertTrue(mine.isdisjoint(theirs))

    def test_a_transient_failure_to_log_chat_is_retried_once(self):
        calls = {"n": 0}
        real = idx._sb

        def flaky(method, path, *a, **k):
            if method == "POST" and path == "chat_log" and calls["n"] == 0:
                calls["n"] += 1
                raise idx.StorageError("blip")
            return real(method, path, *a, **k)

        before = len(self.ids("?limit=500"))
        idx._sb = flaky
        try:
            self.u.msg("3 retry")
        finally:
            idx._sb = real
        self.assertEqual(len(self.ids("?limit=500")), before + 2)  # the user row survived the blip


if __name__ == "__main__":
    unittest.main()
