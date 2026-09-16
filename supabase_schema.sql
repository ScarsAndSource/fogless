-- Run this once in your Supabase project's SQL Editor (Dashboard -> SQL Editor -> New query)

create table if not exists transactions (
  id bigint generated always as identity primary key,
  type text not null check (type in ('expense', 'income')),
  amount numeric not null,
  category text not null,
  note text,
  created_at timestamptz not null default now()
);

create table if not exists settings (
  key text primary key,
  value text
);

-- Migration: add payment_method column (skip if already applied)
alter table transactions add column if not exists payment_method text;

create table if not exists aliases (
  note_key text primary key,
  type text not null check (type in ('expense', 'income')),
  category text not null,
  payment_method text,
  created_at timestamptz not null default now()
);
alter table aliases enable row level security;

create table if not exists processed_updates (
  update_id bigint primary key,
  created_at timestamptz not null default now()
);
alter table processed_updates enable row level security;

-- Row Level Security stays enabled with no policies, so only requests using the
-- service_role key (your bot's server-side key) can read/write. The public/anon
-- key -- which this bot never uses -- would be blocked entirely.
alter table transactions enable row level security;
alter table settings enable row level security;

-- ============================================================================
-- Migration: message -> transaction mapping.
-- Lets an EDITED Telegram message reconcile (replace) the transaction(s) it
-- originally logged, instead of the edit silently creating a duplicate
-- alongside the original. Keyed on (chat_id, message_id) since that's what
-- Telegram gives you on both the original message and its edited_message
-- update -- the update_id itself is different for each, which is exactly why
-- the update_id-based idempotency guard alone couldn't catch this case.
-- ============================================================================
create table if not exists message_log (
  chat_id bigint not null,
  message_id bigint not null,
  tx_ids bigint[] not null default '{}',
  updated_at timestamptz not null default now(),
  primary key (chat_id, message_id)
);
alter table message_log enable row level security;

-- ============================================================================
-- Migration: alias confidence tracking.
-- An auto-learned alias (from a single Groq guess on the first-ever mention
-- of a note phrase) used to become a permanent, silent default immediately.
-- Now it needs a second independent parse that agrees on the same category
-- before it's trusted enough to skip Groq ("confirmed"). A manual correction
-- via /alias or the inline buttons is still trusted on the first try, since
-- that's explicit human input, not a guess.
-- ============================================================================
alter table aliases add column if not exists confirmed boolean not null default false;
alter table aliases add column if not exists candidate_count integer not null default 1;

-- Backfill: aliases that already existed before this migration were already
-- being used as trusted fast-path matches in production -- grandfather them
-- in as confirmed rather than silently disabling every alias you'd already
-- taught the bot before today.
update aliases set confirmed = true where confirmed = false;

-- ============================================================================
-- Migration: chat_log table.
-- Persists every user message and assistant reply so the web frontend can
-- restore full conversation history on page load via GET /api/history.
-- ============================================================================
create table if not exists chat_log (
  id         bigint generated always as identity primary key,
  role       text not null check (role in ('user', 'assistant')),
  content    text not null,
  created_at timestamptz not null default now()
);
alter table chat_log enable row level security;

-- ============================================================================
-- Migration: Shared / Debt / Pool money support.
-- 1. Update transactions type constraint to allow 'transfer'
-- 2. Add transfer columns: from_bucket, to_bucket, person, pool, linked_tx_id
-- 3. Create pool_contributions and pool_expenses tables
-- ============================================================================

-- Drop old check constraint on transactions.type if present and re-add to include 'transfer'
alter table transactions drop constraint if exists transactions_type_check;
alter table transactions add constraint transactions_type_check check (type in ('expense', 'income', 'transfer'));

alter table transactions add column if not exists from_bucket text;
alter table transactions add column if not exists to_bucket text;
alter table transactions add column if not exists person text;
alter table transactions add column if not exists pool text;
alter table transactions add column if not exists linked_tx_id bigint;

create table if not exists pool_contributions (
  id bigint generated always as identity primary key,
  pool text not null,
  person text not null,
  amount numeric not null,
  note text,
  created_at timestamptz not null default now()
);
alter table pool_contributions enable row level security;

create table if not exists pool_expenses (
  id bigint generated always as identity primary key,
  pool text not null,
  amount numeric not null,
  category text not null,
  note text,
  created_at timestamptz not null default now()
);
alter table pool_expenses enable row level security;


