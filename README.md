# Fogless

A personal expense tracker that runs off plain text. You type what you spent
(or received), and it gets parsed, categorized, and logged — no forms, no
dropdowns, no required app structure to follow. Built as a Flask serverless
backend on Vercel with a Supabase (Postgres) database, an optional Telegram
bot, and a small web frontend.

## What it does

- **Natural language parsing** — type something like `24 coffee upi` or
  `got 424 upi` and it extracts the amount, category, payment method, and
  whether it's income or an expense. Uses Groq (`openai/gpt-oss-120b`) for
  parsing, with a local regex-based fallback if the API is unavailable or no
  key is set.
- **Multiple transactions in one message** — `120 lunch upi, 50 bus cash,
  200 veggies card` gets split and logged as three separate entries.
- **Income vs. expense detection** — words like "got," "received," "salary,"
  "refund," or "credited" are treated as income signals; everything else
  defaults to expense.
- **Voice input** — record a short voice memo describing a purchase and it's
  transcribed via Groq Whisper (`whisper-large-v3-turbo`) and parsed the same
  way as typed text.
- **Slash commands** — `/stats`, `/balance`, `/history`, `/undo`, `/delete`,
  `/aliases`, `/reset` for quick ledger operations without needing full
  sentences.
- **Telegram bot** — the same parsing and command logic is available through
  a Telegram webhook, so you can log expenses by messaging the bot directly
  instead of opening the web app.
- **Per-method balances** — tracks running balances separately for cash,
  UPI, card, and netbanking, plus a combined total.
- **Works without a database** — if Supabase isn't configured, it falls back
  to in-memory storage, useful for local testing.

## How it works

You send a message — through the web UI or Telegram — and it's routed
through the same parsing pipeline either way. If it starts with `/`, it's
treated as a command. Otherwise it goes to Groq (or the fallback parser) to
extract one or more transactions, which get written to Supabase and reflected
immediately in your balance totals.

Example:

```
110 Food upi 100 Juice cash 100 biscuit
```

gets parsed into three separate transactions, each logged individually.

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | HTML5, vanilla JS, CSS3, Tailwind |
| Backend | Python Flask, deployed as a Vercel serverless function |
| LLM parsing | Groq API (`openai/gpt-oss-120b`), with a rule-based fallback parser |
| Speech-to-text | Groq Whisper (`whisper-large-v3-turbo`) |
| Database | Supabase (Postgres) via REST API, with in-memory fallback |
| Bot | Telegram Bot API (webhook + `sendMessage`) |

## API endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Serves the web app |
| `/api/message` | POST | Parses freeform text or slash commands |
| `/api/state` | GET | Current balance, today's spend, monthly stats, recent history |
| `/api/action` | POST | Inline edits — undo, delete, change category/payment method |
| `/api/voice` | POST | Accepts an audio file for transcription + logging |
| `/api/transactions` | GET | Lists transactions, filterable by payment method |
| `/api/balances` | GET | Balances broken down by payment method |
| `/api/aliases` | GET | Learned shorthand aliases (e.g. "biscuit" → food/UPI) |
| `/api/history` | GET | Persisted chat log (user + assistant turns) |
| `/api/export` | GET | CSV export of all transactions |
| `/api/backup` | GET | Full JSON dump of transactions + starting balances |
| `/webhook` | POST | Telegram webhook receiver |

## Setup

### Requirements

- Python 3.9+
- A Groq API key (optional — enables LLM parsing and voice transcription;
  without it, a local rule-based parser is used instead)
- A Supabase project (optional — enables persistent storage; without it,
  data lives in memory and resets on restart)
- A Telegram bot token (optional — enables the Telegram integration)

### Install

```bash
git clone https://github.com/ScarsAndSource/fogless.git
cd fogless
pip install -r requirements.txt
```

### Environment variables

Create a `.env` file in the project root:

```
BOT_TOKEN=your_telegram_bot_token
OWNER_ID=your_telegram_chat_id
TELEGRAM_SECRET_TOKEN=your_optional_secret_token
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your_supabase_service_role_key
GROQ_API_KEY=gsk_your_groq_api_key
```

All of these are optional — the app degrades gracefully without them, though
you'll lose the corresponding feature (LLM parsing, persistence, or the
Telegram bot).

### Run locally

```bash
python api/index.py
```

Open `http://localhost:5000`.
