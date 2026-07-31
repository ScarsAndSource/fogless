# Fogless

**Shared accountability for people who live together.**

Fogless started as a way to log your own expenses without ever opening an app. This is what it becomes when "who owes what" stops being a one-person question. It keeps the thing that already worked, you say what happened, in your own words, and it gets logged correctly, and extends it to a group: shared expenses that settle themselves, and commitments that don't get quietly forgotten.

No forms to fill, no spreadsheet to maintain, no one person stuck being "the guy who tracks the rent." You say what happened. The ledger and the reminders take care of themselves.

---

## Why this exists

Every hostel room, flat share, or trip group ends up with the same two failure modes.

1. **Money.** Someone fronts the rent, someone else buys groceries, a third person covers the cab, and within two weeks nobody actually knows who owes whom. The usual fix (a shared spreadsheet, or an app like Splitwise) still asks you to stop and enter the transaction: pick a category, pick who's involved, hit save. That translation step is friction, and friction is why the spreadsheet dies in a week.
2. **Commitments.** "I'll clean the kitchen by Friday." "I'll pay you back by the 1st." No app enforces this. It's either awkward group chat nagging or it just doesn't happen.

Fogless treats both as the same underlying problem: an obligation, with a deadline, that either gets honored or gets escalated. Money and tasks are two flavors of the same thing under the hood.

---

## How it feels to use

You pay the electricity bill for the flat. You don't open an app and navigate to "Add Expense." You type, in the webapp's single input box, exactly like you'd say it out loud:

> `1200 electricity bill upi, split with rohan and aman`

It's parsed, logged, and split three ways before you've closed the tab. No dropdowns, no "select participants" screen.

A week later, the settlement view doesn't show you a tangle of five separate IOUs. It shows you the minimum number of payments needed to make everyone even. Sometimes that's one transfer instead of four.

Someone commits to a task with a deadline. If it's missed, they get a quiet nudge first. If it's still missed, it becomes visible to the group. If it's still missed after that, it goes straight to whoever's named as the accountability partner for that task, delivered as a direct Telegram message, because that's the one channel that reaches someone without requiring them to open anything.

Every entry, money or task, is written once and never silently edited. If a balance looks wrong, you can trace exactly how it got there.

---

## What makes it different

- **The input never got more complicated.** Adding a group didn't mean adding forms. You still just describe what happened.
- **It learns you, not just the group.** Categorization is remembered per person, so your shorthand doesn't clutter someone else's suggestions. Shared items like rent or wifi are recognized at the group level.
- **Debts don't pile up as noise.** The system doesn't show you every individual IOU. It computes the smallest set of payments that clears the group's balance sheet.
- **Nobody can quietly rewrite the ledger.** Every transaction is chained to the one before it. An edited amount after the fact breaks the chain and is detectable.
- **Flaking has consequences that scale.** Escalation isn't a single reminder, it's a graduated sequence, and it applies identically whether the broken commitment was a chore or a payment.
- **Telegram is a notification channel, not a data entry form.** It reaches you the same way a friend's text would, without asking you to open an app you'll eventually stop opening.

---

## Under the hood

Fogless separates how things get in from how things get delivered, which is the core architectural change from the original version. The original used Telegram for both. Now the webapp handles input, and Telegram is used exclusively for escalation delivery.

### Stack

| Layer | Technology |
|---|---|
| Frontend | React + TypeScript (group dashboard, settlement view, task timeline) |
| Backend | FastAPI |
| Language understanding | Groq (`llama-3.3-70b-versatile`) for parsing free text entries |
| Speech to text | Groq Whisper (`whisper-large-v3-turbo`) for voice input |
| Database | Supabase (Postgres) |
| Escalation delivery | Telegram Bot API (webhook, delivery only, not ingestion) |
| Scheduling | Scheduled job (Cloud Run / GitHub Actions) driving the escalation engine |
| Deploy | Cloud Run |

### How an entry becomes a ledger record

1. Text (typed or transcribed from voice) is sent to the parser.
2. Known items are matched against a table of learned aliases, scoped first to the individual, then to the group, before falling back to a full parse.
3. Unmatched or multi part entries are sent to Groq, which returns structured data for every transaction found in the message: amount, category, payment method, payer, and split.
4. Each transaction is written to the ledger with a hash linking it to the previous entry, so the sequence is tamper evident.
5. Per person and per group balances are recalculated from the full transaction history.

### The algorithmic core

This is deliberately the opposite of an LLM wrapper. Groq only ever touches the parsing step, it never decides who owes whom or when to escalate. Two components own that:

- **Settlement engine.** A greedy min cash flow algorithm over a directed, weighted graph of debts, run on demand as a read only view. It never writes to the ledger, it only proposes. Actual repayment is logged as its own transaction, through the same parser, so the ledger stays append only and auditable.
- **Escalation engine.** A finite state machine (`pending to nudged to escalated to resolved`) with configurable per tier delay and target. A missed task and an unpaid debt past threshold are both just "obligation" objects that plug into this one state machine, one reusable component, two features.

### Data model

- `users`: identity, linked Telegram chat ID for escalation delivery.
- `groups` / `group_members`: group membership.
- `transactions`: every expense and income entry, amount, category, payment method, payer, split, settlement status, and hash chain fields (`prev_hash`, `hash`).
- `aliases`: learned mappings from a note to its category and payment method, scoped per user with group level fallback.
- `obligations`: unified record for tasks and outstanding debts, type, deadline, current escalation tier, linked entity.
- `settings`: starting balances per user, per payment method.

### Access control

- Group membership is enforced with row level security. Only members of a group can read or write that group's data.
- The Telegram webhook accepts escalation callbacks only (fixing a category, confirming a payment) and validates a secret token on every request.
- Service credentials are never exposed client side. All writes go through the backend.

---

## The idea behind it

Shared living always carries the same quiet tax: someone has to be the tracker, the nagger, the one doing mental math about who paid for what three weeks ago. Every attempt to fix that adds more structure, more categories, more manual splitting screens, more spreadsheets nobody updates past week two.

Fogless keeps the input as close to zero as it always was, and moves all the structure to where it belongs: underneath, in an algorithm that settles debts on its own and a state machine that doesn't forget a deadline.

You don't manage the ledger. You just say what happened, and the group stays even.
