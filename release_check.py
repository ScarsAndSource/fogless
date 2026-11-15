"""Run before every release:  python release_check.py   (exit code 1 = do not ship)"""
import os, re, sys
ROOT = os.path.dirname(os.path.abspath(__file__))
bad = []
priv = open(os.path.join(ROOT, "frontend", "privacy.html"), encoding="utf-8").read()
if "REPLACE-WITH-YOUR-EMAIL" in priv or "example.com" in priv:
    bad.append("frontend/privacy.html still has the placeholder contact email")
for stale in ("003_safety_and_speed.sql", "final_patch.py", "apply_fixes.py", "fix_frontend.py", ".env"):
    if os.path.exists(os.path.join(ROOT, stale)):
        bad.append(f"{stale} should not be in the repo")
for base, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "tests", "__pycache__", ".pytest_cache")]
    for f in files:
        if f.endswith((".pyc", ".zip")):
            continue
        try:
            t = open(os.path.join(base, f), encoding="utf-8").read()
        except Exception:
            continue
        if re.search(r"eyJ[\w-]{30,}\.[\w-]{10,}\.|sb_secret_\w{12,}|gsk_\w{20,}", t):
            bad.append(f"secret-looking string in {os.path.join(base, f)}")
print("\n".join(bad) or "release_check: OK")
sys.exit(1 if bad else 0)
