# Fogless

A personal expense tracker that runs off plain text. You type what you spent (or received) and it is parsed,
categorised and logged -- no forms, no dropdowns. Flask on Vercel, Supabase (Postgres + Auth), optional Groq for
natural-language parsing and voice.

**Multi-user:** every person signs up with their own email + password and gets a completely private ledger.

## What it does

- **Natural language** -- `24 coffee upi`, `got 424 upi`, `120 lunch upi, 50 bus cash, 200 veggies card`.
  Groq (`openai/gpt-oss-120b`) parses; a local regex parser takes over if there is no key or the API fails.
- **Splits, IOUs, pools** -- `600 dinner split with rahul and amit`, `gave rahul 200 cash`, `put 500 into goa pool`.
- **Voice** -- short voice memos are transcribed with Groq Whisper and logged the same way.
- **Commands** -- `/stats`, `/balance`, `/setbalance`, `/history`, `/undo`, `/delete`, `/debts`, `/pool`, `/settle`,
  `/aliases`, `/reset confirm`.
- **Per-method balances** -- cash / UPI / card / netbanking, plus a total.
- **Accounts** -- sign up, sign in, sign out, forgot/reset password, delete account (with all data).

## How accounts and privacy work

| Piece | What it does |
|---|---|
| Identity | Supabase Auth (email + password). The backend proxies sign-in, so the browser only ever holds two **HttpOnly, SameSite=Lax** cookies (`fl_at` access token, `fl_rt` refresh token). No tokens in JavaScript or `localStorage`. |
| Every API call | Validates the access token with Supabase (cached 20 s), silently refreshes it when expired, then pins the user id. Missing / invalid session -> `401`. |
| Data isolation | All database access goes through one function, `_sb()`. It forces `user_id = <you>` on every read / update / delete, stamps your id on every row written, and refuses any table that is not on an allow-list. Call sites cannot forget the filter because they never write it. |
| Database safety net | Row Level Security on every table (`user_id = auth.uid()`), a per-user `bucket_balances(p_user_id)` function callable only with the service key, and `ON DELETE CASCADE` from `auth.users`. |
| Abuse | Per-user message caps (counted in `chat_log`, so they hold across serverless instances), optional invite code, same-origin check on state-changing requests, `Cache-Control: no-store` on `/api/*`. |
| Timezones | Each account keeps its own timezone (taken from the browser at sign-up / sign-in). "Today" and displayed times follow it; accounts without one keep IST. |

### Trade-offs you should know about

1. **The server uses the service-role key, which bypasses RLS.** Isolation therefore depends on `_sb()` being the only
   door to the database. The test-suite checks this by construction (and fails loudly if the filter is removed), but a
   future developer who calls `requests` against Supabase directly would bypass it. RLS policies are the second line of
   defence for anything that ever uses a user token. The alternative (run every query with the *user's own* JWT so RLS is
   the primary control) is stronger but needs the anon key on every data call and careful token refresh handling.
2. **Sign-in is proxied through your backend**, so Supabase sees your server's IP for every login and applies its
   per-IP rate limits to *all* users together. Fine for a friends-and-family app; if you grow, move sign-in to the browser
   (supabase-js) and have the API accept the Bearer token.
3. **A signed-out token can keep working for up to ~20 s on a warm serverless instance** (the verification cache).
   Logout also revokes the session at Supabase, so it cannot be refreshed.
4. **Static files bypass Flask on Vercel**, so security headers (`X-Frame-Options` etc.) are only set on `/api/*` and
   on pages Flask serves itself. Add a `headers` block to `vercel.json` if you want them everywhere. No CSP is set
   because the pages use inline scripts.

## Supabase setup (one time)

1. **Authentication -> Providers -> Email**: enable. Set the minimum password length to **8** or more.
2. **Authentication -> URL Configuration**: set *Site URL* to your deployment and add `https://YOUR-APP/reset.html`
   to *Redirect URLs*.
3. **Email delivery -- choose one:**
   - *Email confirmation ON (recommended for public use)*: you **must** configure **custom SMTP** (Authentication ->
     Emails -> SMTP Settings; Brevo, Resend, etc.). Supabase's built-in sender only delivers to addresses on your
     Supabase team and is capped at roughly 2 emails/hour, so real users would never receive their confirmation or
     reset emails. Raise the auth email rate limit after enabling SMTP.
   - *Email confirmation OFF (friends-only apps)*: users are signed in immediately. Anyone can register with any
     address, so set `SIGNUP_CODE`. Password reset still needs working email delivery.
4. Copy the **Project URL**, **anon key** and **service_role key** into your environment (see `.env.example`).

## Database

**New project:** run `supabase_schema.sql` once. Done.

**Already running the single-user version (your data is kept):**

1. Run `003_multiuser_1_prepare.sql` (adds a nullable `user_id`; the old app keeps working).
2. Deploy this version and **create your own account** (sign up in the app, or Dashboard -> Authentication -> Users).
3. Edit the `owner_email` line in `003_multiuser_2_finalize.sql` to that email and run it. It assigns every existing
   row to you, makes `user_id` mandatory, fixes the keys, adds indexes and turns on RLS. It runs in one transaction
   and refuses to do anything if the account does not exist.
4. Run `004_safety_and_speed.sql` (per-user balances function + no-zero-amount guards). Optional: without it balances
   are computed by downloading the ledger, which is slower.

Until step 3 you will see an empty ledger (your rows are not lost, just not yet attached to your account).
`message_log` and `processed_updates` are leftovers from the removed Telegram bot; nothing uses them and they can be dropped.

## Environment variables

Both legacy (JWT `anon` / `service_role`) and new-style (`sb_publishable_…` / `sb_secret_…`) Supabase keys work; the new-style ones are sent only in the `apikey` header, as Supabase requires.

See `.env.example`. `SUPABASE_URL`, `SUPABASE_ANON_KEY` and `SUPABASE_SERVICE_KEY` are required; without them every
sign-in attempt returns `503` (it fails closed). `ALLOW_RESET`, `DEMO_USERNAME`, `DEMO_PASSWORD`, `SESSION_SECRET`
and `CRON_SECRET` no longer exist -- remove them from Vercel.

## Run locally

```bash
pip install -r requirements.txt
cp .env.example .env      # fill it in (a .env in the project root is loaded automatically; real env vars win)
python api/index.py       # http://127.0.0.1:5000
```

No Supabase? `FOGLESS_DEV_MODE=1 python api/index.py` gives you in-memory accounts and data (lost on restart, ignored
on Vercel and whenever Supabase is configured). A password-reset "email" is printed to the console.

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

No network and no real keys needed (`.env` is never read under test). The suite runs the real Flask app against a small
fake Supabase (Auth + PostgREST semantics) and covers: sign-up / sign-in / refresh / logout / reset / delete, email
confirmation, invite codes, CSRF, outage handling, and -- most importantly -- that user A can never read, edit, delete,
undo, export, reset or rate-limit user B. It also covers the parsing and ledger fixes and the Supabase-only code paths.

## API

All routes except the public auth ones require a signed-in session.

| Endpoint | Method | Description |
|---|---|---|
| `/api/config` | GET | Public: whether sign-up is open / needs an invite code |
| `/api/signup` `/api/login` `/api/logout` | POST | Create account / sign in / sign out |
| `/api/session` | GET | Public: `{authenticated, email}` |
| `/api/forgot` `/api/reset-password` | POST | Request a reset email / set a new password from the emailed link |
| `/api/account/delete` | POST | Erase your data and your account (needs your password) |
| `/api/message` | POST | Freeform text or slash commands (rate-limited) |
| `/api/voice` | POST | Audio -> transcription -> log (rate-limited) |
| `/api/state` `/api/balances` `/api/transactions` `/api/aliases` `/api/history` | GET | Your data |
| `/api/action` | POST | undo / delete / change category or payment method |
| `/api/export` `/api/backup` | GET | CSV / JSON of **your** data |
