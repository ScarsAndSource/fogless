# Personal Expense Tracker Bot — Serverless / Zero-Card Edition

Same bot, different backbone: instead of a VM you keep alive yourself, this version
has **no server to manage at all**. Telegram sends each message straight to a
Vercel function, which wakes up in milliseconds, does the work, replies, and shuts
back down. There's nothing idling, so there's nothing to crash and nothing that
needs "always on."

No credit card is required anywhere in this stack.

## Commands
Same as before: `/add`, `/income`, `/balance`, `/stats`, `/history`, `/undo`, `/delete`,
`/setbalance`, `/export` (CSV), `/backup` (JSON), `/reset confirm`, `/help`.

---

## Step 1 — Create the bot on Telegram
1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts, copy the **token**.
2. Message [@userinfobot](https://t.me/userinfobot) to get your numeric **Telegram user ID**.

## Step 2 — Create the free database (Supabase)
1. Sign up at [supabase.com](https://supabase.com) — email only, no card.
2. Create a new project (pick any name/region, set a database password — you won't need it directly).
3. Once it's ready: **SQL Editor -> New query** -> paste the contents of `supabase_schema.sql` from this project -> Run.
4. Go to **Project Settings -> API** and copy:
   - **Project URL** -> this is `SUPABASE_URL`
   - **service_role key** (not the `anon` key — the secret one) -> this is `SUPABASE_SERVICE_KEY`

## Step 3 — Push this project to GitHub
Vercel deploys straight from a GitHub repo.
```bash
cd expense-bot-vercel
git init
git add .
git commit -m "Expense tracker bot"
```
Create a new empty repo on GitHub (you already have the workflow, ScarsAndSource), then:
```bash
git remote add origin https://github.com/<you>/expense-bot.git
git push -u origin main
```

## Step 4 — Deploy to Vercel
1. Sign up at [vercel.com](https://vercel.com) with your GitHub account — no card.
2. **Add New -> Project**, import the repo you just pushed.
3. Before deploying, open **Environment Variables** and add all five from `.env.example`:
   `BOT_TOKEN`, `OWNER_ID`, `TELEGRAM_SECRET_TOKEN`, `CRON_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
   (make up your own random strings for the two secret tokens — a password generator is fine).
4. Deploy. You'll get a URL like `https://expense-bot-yourname.vercel.app`.

## Step 5 — Point Telegram at your bot
One-time call to register the webhook (run this from your own machine, or Vercel's own
console — no persistent server involved, just a single request):
```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-vercel-domain>/webhook" \
  -d "secret_token=<your TELEGRAM_SECRET_TOKEN>"
```
You should get `{"ok":true,...}` back. Now message your bot on Telegram — try `/start`,
then `/setbalance 1000`, then `/add 100 food`.

## Step 6 — Keep Supabase from pausing (fully automated)
Supabase's free tier pauses a project after 7 days with zero database activity.
The included GitHub Actions workflow (`.github/workflows/keepalive-backup.yml`) runs
daily, hits your bot's `/cron/backup` endpoint, which both touches the database
(resetting the 7-day clock) **and** sends you a fresh JSON backup on Telegram —
so you're covered even if you don't use the bot for a while.

To activate it, add two repo secrets: **GitHub repo -> Settings -> Secrets and
variables -> Actions -> New repository secret**:
- `VERCEL_DOMAIN` -> e.g. `expense-bot-yourname.vercel.app` (no `https://`)
- `CRON_SECRET` -> same value you set in Vercel's env vars

That's it — the workflow runs automatically from here, and you can also trigger it
manually anytime from the repo's **Actions** tab.

---

## Why this is safe for "just you"
- Every Telegram message is checked against `OWNER_ID` before anything runs — anyone else
  who messages the bot gets silently ignored, so it doesn't even reveal it's a working bot.
- The webhook itself is checked against `TELEGRAM_SECRET_TOKEN` on every request, so even
  if someone finds your Vercel URL, they can't fake Telegram messages to it.
- Supabase's Row Level Security is on with no public policies — only the `service_role`
  key (which only your Vercel function holds, as an env var, never in code) can touch
  your data at all.
- Every command is wrapped so a bad input (like a typo'd amount) replies with a normal
  error message instead of the function crashing.

## Free-tier ceilings, for peace of mind
For single-user personal use, you'd need to be logging hundreds of transactions a day
for years to get near any of these:
- Vercel Hobby: ~100,000 function invocations/month, 100GB bandwidth
- Supabase free: 500MB database (a lifetime of expense entries is a few MB), 5GB bandwidth
- GitHub Actions: 2,000 free minutes/month on private repos (this job uses ~5 seconds/day)
