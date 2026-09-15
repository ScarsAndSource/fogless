# Fogless — 16-Bit JRPG Personal Finance Companion

**Fogless** is a 16-bit JRPG-inspired personal expense tracker and finance companion web application with a serverless API backend. Powered by AI natural language extraction, multi-transaction parsing, voice memo logging, and Telegram integration, Fogless makes managing your daily pouch as intuitive as typing or speaking out loud.

Chat with **Extreme**, your calm budget familiar, log multi-item transactions in plain text, track treasury pouch balances per payment mode (UPI, Cash, Card, Netbanking), and run sanctuary spells to manage your finances.

---

## Key Features

- ⚔️ **16-Bit JRPG Sanctuary Interface**: Full-screen responsive retro dashboard with lo-fi aesthetics, dynamic pace meters, interactive avatar (**Extreme**), and retro sound effects via Web Audio API.
- 💬 **Natural Language Multi-Transaction Parsing**: Log single or sequential transactions in plain text (e.g., `110 Food upi 100 Juice cash 100 biscuit` or `got 424 upi`). Powered by Groq (`openai/gpt-oss-120b`) with a local regex rule parser fallback.
- 💰 **Smart Income & Expense Classification**: Instant detection of income signals (`got`, `got paid`, `credited`, `salary`, `refund`) vs expenses, formatted in ₹ (INR).
- 🎙️ **Voice Memo Cassette Recorder**: Tap the mic button to speak your expenses out loud; transcribed and extracted automatically via Groq Whisper (`whisper-large-v3-turbo`).
- 📜 **Sanctuary Spells (Commands)**: Quick slash commands (`/stats`, `/balance`, `/history`, `/undo`, `/delete`, `/aliases`, `/reset`) for real-time ledger management.
- 📱 **Telegram Bot Integration**: Full webhook support (`/webhook`) that receives messages and replies back with clean plain-text balance and spend summaries using the Telegram Bot API.
- 💾 **Dual-Mode Persistence**: Hybrid Supabase (Postgres) database integration with zero-config in-memory fallback for offline or local development.
- 🎨 **Global Font Consistency**: Unified `Space Mono` typography across every panel and bubble component.

---

## How It Works

### 1. Plain Text Entry
You pay for coffee or receive a refund. You don't open an app to fill forms or navigate dropdowns. You type in the single terminal prompt:
> `24 coffee upi` or `got 424 upi`

It's parsed, categorized, and logged to your pouch instantly.

### 2. Multi-Log in One Go
Got groceries, lunch, and a bus ticket in one trip? Type them together:
> `120 lunch upi, 50 bus cash, 200 veggies card`

Every transaction is extracted separately into your ledger.

### 3. Voice Memo Logging
Tap the 🎙️ cassette mic button, speak your expense, and let Groq Whisper transcribe and log it to the treasury automatically.

---

## Under the Hood

### Technology Stack

| Layer | Technology |
|---|---|
| **Frontend** | Responsive 16-Bit JRPG UI (HTML5, Vanilla JS, CSS3, TailwindCSS, Web Audio API synth) |
| **Backend** | Python Flask Serverless (Vercel API / Local) |
| **LLM Intelligence** | Groq API (`openai/gpt-oss-120b`) & Heuristic Rule Engine Fallback |
| **Speech-to-Text** | Groq Whisper API (`whisper-large-v3-turbo`) & Browser `MediaRecorder` API |
| **Database** | Supabase REST API (Postgres) with in-memory fallback |
| **Bot Delivery** | Telegram Bot API (`sendMessage`) & Webhook Receiver |

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Serves the main sanctuary web application. |
| `/api/message` | `POST` | Processes natural language expense input or slash commands. |
| `/api/state` | `GET` | Fetches current balance, today's outflow, monthly stats, and recent history. |
| `/api/action` | `POST` | Executes inline record edits (`undo`, `delete`, `set_category`, `set_payment`). |
| `/api/voice` | `POST` | Accepts audio file uploads for Whisper STT transcription and logging. |
| `/api/transactions` | `GET` | Lists transactions (filterable by payment method). |
| `/api/balances` | `GET` | Returns treasury balances broken down by payment method. |
| `/api/aliases` | `GET` | Retrieves all auto-learned and confirmed expense shorthand aliases. |
| `/api/history` | `GET` | Retrieves persisted chat turns (user + assistant). |
| `/api/export` | `GET` | Exports all transaction history as a CSV file. |
| `/api/backup` | `GET` | Exports complete transaction and starting balance state as JSON. |
| `/webhook` | `POST` | Telegram Bot Webhook endpoint for receiving and replying to chat updates. |

---

## Getting Started

### 1. Prerequisites
- Python 3.9+
- Groq API Key (Optional, for LLM parsing & Whisper STT)
- Supabase Project (Optional, for cloud persistence)
- Telegram Bot Token (Optional, for Telegram bot integration)

### 2. Installation
```bash
git clone https://github.com/ScarsAndSource/fogless.git
cd fogless
pip install -r requirements.txt
```

### 3. Environment Setup
Create a `.env` file in the root directory:
```env
BOT_TOKEN=your_telegram_bot_token
OWNER_ID=your_telegram_chat_id
TELEGRAM_SECRET_TOKEN=your_optional_secret_token
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your_supabase_service_role_key
GROQ_API_KEY=gsk_your_groq_api_key
```

### 4. Running Locally
```bash
python api/index.py
```
Open [http://localhost:5000](http://localhost:5000) in your browser to enter the Sanctuary.

---

## License

MIT License. Designed for personal finance logging with 16-bit JRPG nostalgia.
