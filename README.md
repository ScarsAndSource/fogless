# Fogless

**The expense tracker that gets out of your way.**

Fogless is not an app you open, navigate, and fill out. It is a chat you already have open, and a bot that understands what you mean the first time you say it. You type what happened, in your own words, and the ledger takes care of itself.

No forms. No dropdowns. No onboarding screens. Just clarity, instantly.

---

## Why "Fogless"

Every expense tracker on the market asks you to translate your life into their structure: pick a category, select a date, choose a currency, confirm the entry. That translation step is the fog. It is the small, constant tax between something happening and it being recorded.

Fogless removes the translation step entirely. You say what happened, exactly as you'd say it to a friend, and it is already logged, categorized, and filed. Nothing stands between the moment and the record of it.

---

## How it feels to use

You are standing in line, and you just paid for milk on UPI. You do not open an app. You type:

> `60 milk upi`

It is done. Categorized, tagged with the payment method, balance recalculated, before your thumb even leaves the screen.

You are rushing, three purchases deep before you remember to log the first one. You do not log them one at a time. You say all of it at once, exactly as it happened:

> `400 creatine cash, 60 milk upi, 1200 rent upi`

All three are sorted, filed, and confirmed in the time it takes to read the sentence back.

Your hands are full, and typing is not an option. You send a voice note instead, the same way you'd leave one for a friend. Fogless listens, transcribes, understands, and logs it, exactly as if you had typed it yourself.

---

## What makes it different

1. **It speaks your language, not the other way around.**
   No categories to memorize, no syntax to learn. You describe the transaction the way you naturally would, and Fogless does the parsing.

2. **It learns you.**
   Mention "creatine" once, and it never asks about "creatine" again. Categorization is remembered, not re-guessed, so every repeat entry is instant, free, and perfectly consistent.

3. **It corrects itself without breaking your flow.**
   Got something wrong? Fix it with a single tap on the confirmation message itself. No retyping, no separate undo command to remember.

4. **It never repeats itself by accident.**
   Every message is checked the instant it arrives. If the network stutters and the same message is delivered twice, only one of them ever becomes a transaction.

5. **It handles more than one thing at once.**
   Multiple purchases, one message. Fogless splits, categorizes, and logs each one individually.

6. **It answers to exactly one person.**
   Fogless is built for a single thread of thought. Every other sender is treated as if their message never arrived at all.

---

## Under the hood

Fogless has no server that needs to be kept alive. Every message from Telegram arrives as a webhook call to a serverless function, which wakes up, does its work, replies, and shuts back down. There is nothing idling and nothing that needs monitoring.

**Stack**

| Layer | Technology |
|---|---|
| Runtime | Vercel serverless functions (Python, Flask) |
| Messaging | Telegram Bot API (webhook, not polling) |
| Language understanding | Groq (`llama-3.3-70b-versatile`) for text parsing |
| Speech-to-text | Groq Whisper (`whisper-large-v3-turbo`) for voice notes |
| Database | Supabase (Postgres, accessed via its REST API) |
| Scheduling | GitHub Actions, once daily |
| Structure | Single-file deployment (no module bundling issues) |

**How a message becomes a transaction**

1. Telegram delivers the message to a `/webhook` endpoint over HTTPS, signed with a secret token so only genuine Telegram traffic is accepted.
2. The update is checked against a table of already-processed update IDs, so a retried delivery never creates a duplicate entry.
3. If the message is text, it is checked against a locally stored table of learned aliases first. A known item is logged instantly with no external call at all.
4. If the message is new, unclear, or contains several transactions at once, it is sent to Groq, which returns structured data: type, amount, category, payment method, and note, for every transaction found.
5. If the message is a voice note, it is transcribed by Groq's Whisper model first, then handled exactly as text from that point on.
6. Each transaction is written to Supabase and a confirmation is sent back with inline buttons to fix the category, change the payment method, or undo the entry.
7. The running balance is recalculated per payment method from the full transaction history plus that method's stored starting balance.

**Data model**

- `transactions`: every expense and income entry, with type, amount, category, payment method, note, and timestamp.
- `settings`: stored starting balances per payment method (e.g. `starting_balance:cash`, `starting_balance:upi`), used as the baseline for per-method balance calculations.
- `aliases`: learned mappings from a note (like "creatine") to its category and payment method, built automatically from usage and corrections.
- `processed_updates`: a short-lived log of Telegram update IDs, used only to guarantee no message is ever logged twice.

**Access control**

- Every incoming message is checked against a single, fixed Telegram user ID before anything runs. Any other sender is silently ignored.
- The webhook itself only accepts requests carrying the correct secret token, so the endpoint cannot be triggered by a guessed URL.
- The database has row-level security enabled with no public policies, so only the server's own service key, never exposed in code, can read or write data.
- A separate daily job keeps the database active and sends a full backup, so nothing is ever lost even during long stretches of inactivity.

---

## The idea behind it

Expense tracking has always carried a quiet cost: the more structure an app demands, the less honestly people actually use it. Every attempt to fix that has added more categories, more automation, more dashboards that get opened once and never used again.

Fogless takes the opposite approach: less structure, not more. The words you'd already say out loud become the entire input, and everything downstream of that happens without you ever noticing the machinery turning.

You do not log expenses anymore. You talk about your day, and the numbers take care of themselves.
