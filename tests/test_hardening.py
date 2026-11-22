"""Tests for the pre-launch hardening pass: abuse throttles, security headers, error hygiene, XSS, dependency pins."""
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from supa import FAKE, idx, User, PW, new_email, Base  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ip(addr):
    return {"X-Forwarded-For": addr}


class TestLoginThrottle(Base):
    def test_wrong_passwords_are_capped_per_ip_and_other_ips_are_unaffected(self):
        u = User()
        c = idx.app.test_client()
        for _ in range(idx.AUTH_FAILS_PER_5MIN):
            r = c.post("/api/login", json={"email": u.email, "password": "nope-nope-nope"}, headers=ip("9.9.9.9"))
            self.assertEqual(r.status_code, 401)
        blocked = c.post("/api/login", json={"email": u.email, "password": PW}, headers=ip("9.9.9.9"))
        self.assertEqual(blocked.status_code, 429)  # even the RIGHT password is refused while blocked
        ok = c.post("/api/login", json={"email": u.email, "password": PW}, headers=ip("8.8.8.8"))
        self.assertEqual(ok.status_code, 200)  # a different address is not punished

    def test_successful_logins_are_never_counted(self):
        u = User()
        for _ in range(idx.AUTH_FAILS_PER_5MIN + 5):
            c = idx.app.test_client()
            r = c.post("/api/login", json={"email": u.email, "password": PW}, headers=ip("7.7.7.7"))
            self.assertEqual(r.status_code, 200)

    def test_window_expires(self):
        u = User()
        c = idx.app.test_client()
        for _ in range(idx.AUTH_FAILS_PER_5MIN):
            c.post("/api/login", json={"email": u.email, "password": "bad-bad-bad"}, headers=ip("6.6.6.6"))
        self.assertEqual(c.post("/api/login", json={"email": u.email, "password": PW}, headers=ip("6.6.6.6")).status_code, 429)
        real = idx.time.monotonic
        with mock.patch.object(idx.time, "monotonic", lambda: real() + 301):
            self.assertEqual(c.post("/api/login", json={"email": u.email, "password": PW}, headers=ip("6.6.6.6")).status_code, 200)


class TestSignupAndForgotThrottle(Base):
    def test_signup_attempts_are_capped_per_ip(self):
        c = idx.app.test_client()
        codes = [c.post("/api/signup", json={"email": new_email(), "password": PW}, headers=ip("5.5.5.5")).status_code
                 for _ in range(idx.SIGNUPS_PER_IP_PER_10MIN + 2)]
        self.assertEqual(codes[:idx.SIGNUPS_PER_IP_PER_10MIN], [200] * idx.SIGNUPS_PER_IP_PER_10MIN)
        self.assertEqual(codes[idx.SIGNUPS_PER_IP_PER_10MIN:], [429, 429])
        self.assertEqual(c.post("/api/signup", json={"email": new_email(), "password": PW}, headers=ip("4.4.4.4")).status_code, 200)

    def test_invite_code_guessing_is_throttled(self):
        idx.SIGNUP_CODE = "letmein"
        c = idx.app.test_client()
        codes = [c.post("/api/signup", json={"email": new_email(), "password": PW, "code": f"guess{i}"},
                        headers=ip("3.3.3.3")).status_code for i in range(idx.SIGNUPS_PER_IP_PER_10MIN + 1)]
        self.assertEqual(codes[-1], 429)

    def test_reset_mail_is_limited_but_the_answer_never_changes(self):
        sent = []
        victim = new_email()
        with mock.patch.object(idx, "_auth_recover", lambda email, redirect: sent.append(email) or (200, {})):
            c = idx.app.test_client()
            answers = {json.dumps(c.post("/api/forgot", json={"email": victim}, headers=ip("2.2.2.2")).json)
                       for _ in range(8)}
            unknown = c.post("/api/forgot", json={"email": new_email()}, headers=ip("1.1.1.1")).json
        self.assertEqual(len(answers), 1)               # identical answer every time (no enumeration signal)
        self.assertEqual(answers, {json.dumps(unknown)})  # ...and identical for an unknown address
        self.assertEqual(sent.count(victim), 3)         # but a victim's inbox can't be flooded


class TestVoiceThrottle(Base):
    def test_failing_uploads_still_count(self):
        u = User()
        statuses = []
        for _ in range(idx.VOICE_PER_MIN + 3):
            r = u.c.post("/api/voice", data={"audio": (io.BytesIO(b"abc"), "x.webm")}, content_type="multipart/form-data")
            statuses.append(r.status_code)
        self.assertEqual(statuses[:idx.VOICE_PER_MIN], [502] * idx.VOICE_PER_MIN)  # no Groq key -> transcription fails
        self.assertEqual(statuses[idx.VOICE_PER_MIN:], [429] * 3)

    def test_limit_is_per_user(self):
        a, b = User(), User()
        for _ in range(idx.VOICE_PER_MIN + 1):
            a.c.post("/api/voice", data={"audio": (io.BytesIO(b"abc"), "x.webm")}, content_type="multipart/form-data")
        r = b.c.post("/api/voice", data={"audio": (io.BytesIO(b"abc"), "x.webm")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 502)


class TestDeleteAccountThrottle(Base):
    def test_password_guessing_on_delete_is_capped(self):
        u = User()
        codes = [u.c.post("/api/account/delete", json={"password": f"wrong-pass-{i}"}).status_code for i in range(7)]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5:], [429, 429])
        self.assertEqual(u.c.get("/api/state").status_code, 200)  # still signed in, nothing deleted


class TestHeaders(Base):
    def test_every_response_carries_the_policy(self):
        u = User()
        for path in ("/api/state", "/api/session", "/", "/login", "/privacy.html", "/nope.txt"):
            r = u.c.get(path)
            self.assertEqual(r.headers.get("Content-Security-Policy"), idx.CSP, path)
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY", path)
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff", path)
            self.assertIn("microphone=(self)", r.headers.get("Permissions-Policy", ""), path)

    def test_vercel_json_sends_the_same_policy_for_static_files(self):
        cfg = json.load(open(os.path.join(ROOT, "vercel.json")))
        first = cfg["routes"][0]
        self.assertEqual(first["src"], "/(.*)")
        self.assertTrue(first.get("continue"))  # without this the header route would swallow every request
        h = first["headers"]
        self.assertEqual(h["Content-Security-Policy"], idx.CSP)
        self.assertEqual(h["Permissions-Policy"], idx.PERMISSIONS_POLICY)
        self.assertEqual(h["X-Frame-Options"], "DENY")
        dests = [r.get("dest") for r in cfg["routes"][1:]]
        self.assertEqual(dests, ["/api/index.py", "/frontend/login.html", "/frontend/$1"])  # original routing untouched

    def test_policy_still_allows_what_the_pages_need(self):
        csp = idx.CSP
        self.assertIn("script-src 'self' 'unsafe-inline'", csp)      # inline <script> + onclick="" in the pages
        self.assertIn("https://fonts.googleapis.com", csp)            # style.css @import
        self.assertIn("font-src https://fonts.gstatic.com", csp)
        self.assertIn("media-src 'self' blob:", csp)                  # voice notes
        self.assertIn("connect-src 'self'", csp)                      # all fetch() calls are same-origin
        self.assertIn("frame-ancestors 'none'", csp)

    def test_pages_only_load_from_allowed_origins(self):
        allowed = ("https://fonts.googleapis.com", "https://fonts.gstatic.com", "http://www.w3.org")
        for name in ("index.html", "login.html", "reset.html", "privacy.html", "style.css", "app.js", "auth.js"):
            text = open(os.path.join(ROOT, "frontend", name), encoding="utf-8").read()
            for url in re.findall(r"https?://[^\s\"'<>)]+", text):
                self.assertTrue(url.startswith(allowed), f"{name} loads {url}, which the CSP would block")
            for bad in ("eval(", "new Function", "javascript:", "localStorage", "sessionStorage"):
                self.assertNotIn(bad, text, f"{name} uses {bad}")


class TestErrorHygiene(Base):
    def test_storage_failure_does_not_leak_table_names_or_paths(self):
        u = User()
        FAKE.fail_bulk_transactions = True
        r = u.msg("100 dinner split with a and b")
        self.assertEqual(r.status_code, 503)
        text = r.json["text"]
        for leak in ("transactions", "HTTP", "POST", "rest/v1", "supabase"):
            self.assertNotIn(leak.lower(), text.lower())
        self.assertIn("Database error", text)


class TestXSS(Base):
    PAYLOADS = ["<img src=x onerror=alert(1)> 50 upi", "50 <script>alert(1)</script> cash",
                "gave <svg onload=alert(2)> 30 cash", "put 10 into <b onmouseover=alert(3)> pool",
                "600 dinner split with <img src=x onerror=alert(4)> and amit", "\"><script>alert(5)</script> 20 upi"]

    def test_hostile_input_never_reaches_the_page_as_markup(self):
        idx.RATE_LIMIT_PER_MIN = idx.RATE_LIMIT_PER_DAY = 0
        u = User()
        for p in self.PAYLOADS:
            r = u.msg(p)
            self.assertIn(r.status_code, (200, 400), p)
            blob = (r.json.get("html") or "") + (r.json.get("text") or "")
            self.assertNotRegex(blob, r"<(?:script|img|svg|iframe)\b", p)
            self.assertNotRegex(blob, r"\son\w+\s*=", p)
        for h in u.c.get("/api/history").json:
            if h["role"] == "assistant":  # (user rows are escaped by the page itself)
                self.assertNotRegex(h["content"], r"<(?:script|img|svg|iframe)\b")
                self.assertNotRegex(h["content"], r"\son(?:error|load|mouseover)\s*=")
        # every server-built HTML view that echoes stored notes / names / pool names must escape them
        # (the JSON endpoints return the raw note on purpose: the page never renders those as HTML)
        for cmd in ("/history", "/stats", "/balance", "/debts", "/pool", "/aliases", "/undo"):
            r = u.msg(cmd)
            blob = (r.json.get("html") or "") + (r.json.get("text") or "")
            self.assertNotRegex(blob, r"<(?:script|img|svg|iframe)\b", cmd)
            # a real injected attribute lives inside a tag; hostile text shown as escaped, inert text
            # (the history card now lists enough rows to show it) legitimately contains "onerror=".
            self.assertNotRegex(blob, r"<[^>]*\son\w+\s*=", cmd)

    def test_csv_export_neutralises_formulas(self):
        u = User()
        u.msg("50 =HYPERLINK(\"http://evil\") upi")
        csv_text = u.c.get("/api/export").get_data(as_text=True)
        self.assertNotRegex(csv_text, r"(?m)(^|,)=")


class TestSupplyChain(unittest.TestCase):
    def test_requirements_are_exactly_pinned(self):
        lines = [l.strip() for l in open(os.path.join(ROOT, "requirements.txt")) if l.strip() and not l.startswith("#")]
        self.assertTrue(lines)
        for l in lines:
            self.assertRegex(l, r"^[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9.]*$", l)
        names = {l.split("==")[0].lower() for l in lines}
        self.assertTrue({"flask", "requests", "werkzeug", "tzdata"} <= names)

    def test_no_secrets_committed(self):
        pat = re.compile(r"eyJ[A-Za-z0-9_\-]{30,}\.[A-Za-z0-9_\-]{10,}\.|sb_secret_[A-Za-z0-9]{12,}|gsk_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache", "node_modules", "tests")]
            for f in files:
                if f.endswith((".zip", ".pyc")):
                    continue
                try:
                    text = open(os.path.join(base, f), encoding="utf-8").read()
                except (UnicodeDecodeError, OSError):
                    continue
                self.assertIsNone(pat.search(text), f"possible secret in {os.path.join(base, f)}")

    def test_dangerous_one_off_files_are_gone(self):
        for stale in ("003_safety_and_speed.sql", "final_patch.py", "apply_fixes.py", "fix_frontend.py"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, stale)), stale)


if __name__ == "__main__":
    unittest.main()
