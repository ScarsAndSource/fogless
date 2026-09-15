
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
