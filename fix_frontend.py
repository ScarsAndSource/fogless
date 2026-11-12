"""
fix_frontend.py  --  run ONCE from the project root:   python fix_frontend.py

Patches frontend/app.js. All-or-nothing: every replacement must match exactly the expected number
of times, the result is written only if everything passed. CRLF/LF is preserved.
Undo with:  git checkout frontend/app.js
"""
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "frontend/app.js"
raw = open(PATH, "rb").read().decode("utf-8")
crlf = "\r\n" in raw
src = raw.replace("\r\n", "\n")

if "_fogless_patched" in src:
    sys.exit("Already patched. Nothing changed.")


def rep(old, new, count=1):
    global src
    n = src.count(old)
    if n != count:
        sys.exit(
            f"PATCH FAILED: expected {count} match(es), found {n} for:\n{old[:200]}\nNothing was changed."
        )
    src = src.replace(old, new)


# ------------------------------------------------------------------ 1. mark as patched (sentinel)
rep(
    "// Web Audio 8-bit Synthesizer for Retro Sound Effects",
    "// Web Audio 8-bit Synthesizer for Retro Sound Effects\n// _fogless_patched",
)

# ------------------------------------------------------------------ 2. /api/state: add credentials
rep(
    "      const res = await fetch('/api/state');",
    "      const res = await fetch('/api/state', { credentials: 'same-origin', cache: 'no-store' });",
)

# ------------------------------------------------------------------ 3. /api/history: add credentials
rep(
    "      const res = await fetch('/api/history?limit=50');",
    "      const res = await fetch('/api/history?limit=50', { credentials: 'same-origin', cache: 'no-store' });",
)

# ------------------------------------------------------------------ 4. voice upload: add credentials
rep(
    "      const res = await fetch('/api/voice', { method: 'POST', body: formData });",
    "      const res = await fetch('/api/voice', { method: 'POST', body: formData, credentials: 'same-origin' });",
)

# ------------------------------------------------------------------ 5. handleQuickUndo: guard against non-JSON + 401
rep(
    """    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'undo' })
      });
      const data = await res.json();
      if (data.ok && data.balance !== undefined) updateBalanceDisplay(data.balance);
      if (data.ok && data.today_spent !== undefined) updateTodayPaceDisplay(data.today_spent);
      renderCompanionBubble(data.text || `\u2694 SPELL: REVERT EXECUTED. Restored GP to the treasury purse.`);
    } catch(e) {
      renderCompanionBubble(`\u26a0 Connection error \u2014 undo did NOT go through. Balance unchanged. Try again.`);
    }""",
    """    try {
      const res = await fetch('/api/action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'undo' }),
        credentials: 'same-origin',
      });
      let data = null;
      try { data = await res.json(); } catch (_) { data = null; }
      if (!data) {
        renderCompanionBubble(`\u26a0 Server error (HTTP ${res.status}) \u2014 undo may not have gone through. Check /history.`);
        return;
      }
      if (data.ok && data.balance !== undefined) updateBalanceDisplay(data.balance);
      if (data.ok && data.today_spent !== undefined) updateTodayPaceDisplay(data.today_spent);
      renderCompanionBubble(data.html || data.text || `\u2694 SPELL: REVERT EXECUTED. Restored GP to the treasury purse.`);
    } catch(e) {
      renderCompanionBubble(`\u26a0 Connection error \u2014 undo did NOT go through. Balance unchanged. Try again.`);
    }""",
)

# ------------------------------------------------------------------ 6. _runReadOnlyCommand: add credentials
rep(
    """      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: cmd })
      });""",
    """      const res = await fetch('/api/message', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: cmd }),
        credentials: 'same-origin',
      });""",
)

# ------------------------------------------------------------------ write
out = src.replace("\n", "\r\n") if crlf else src
open(PATH, "wb").write(out.encode("utf-8"))
print("frontend/app.js patched OK.")
