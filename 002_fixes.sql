-- ============================================================================
-- fogless migration 002 -- run ONCE in Supabase: Dashboard -> SQL Editor -> New query.
-- Idempotent: safe to run again, and safe even if an earlier migration never ran.
-- Do NOT deploy the new index.py before running this.
-- ============================================================================

-- 1. Transfers (split / lend / borrow / repay / pool) need 'transfer' as a valid type...
alter table transactions drop constraint if exists transactions_type_check;
alter table transactions add constraint transactions_type_check
  check (type in ('expense', 'income', 'transfer'));

-- ...and their own columns (no-ops if you already have them)
alter table transactions add column if not exists payment_method text;
alter table transactions add column if not exists from_bucket text;
alter table transactions add column if not exists to_bucket text;
alter table transactions add column if not exists person text;
alter table transactions add column if not exists pool text;
alter table transactions add column if not exists linked_tx_id bigint;

-- 2. Belt and braces: transfers have no spending category, so never let NOT NULL reject them.
--    (The new code also writes category = 'transfer', so it works either way.)
alter table transactions alter column category drop not null;

-- 3. Pool tables (no-ops if present)
create table if not exists pool_contributions (
  id bigint generated always as identity primary key,
  pool text not null,
  person text not null,
  amount numeric not null,
  note text,
  created_at timestamptz not null default now()
);
create table if not exists pool_expenses (
  id bigint generated always as identity primary key,
  pool text not null,
  amount numeric not null,
  category text not null,
  note text,
  created_at timestamptz not null default now()
);
alter table pool_contributions enable row level security;
alter table pool_expenses enable row level security;

-- A contribution made by "put N into X pool" remembers the transfer row it belongs to,
-- so /undo and /delete remove both together.
alter table pool_contributions add column if not exists linked_tx_id bigint;

-- 4. Telegram idempotency + edit tracking (no-ops if present)
create table if not exists processed_updates (
  update_id bigint primary key,
  created_at timestamptz not null default now()
);
alter table processed_updates enable row level security;

create table if not exists message_log (
  chat_id bigint not null,
  message_id bigint not null,
  tx_ids bigint[] not null default '{}',
  updated_at timestamptz not null default now(),
  primary key (chat_id, message_id)
);
alter table message_log enable row level security;
-- an edited message may have created pool rows too
alter table message_log add column if not exists pool_contribution_ids bigint[] not null default '{}';
alter table message_log add column if not exists pool_expense_ids bigint[] not null default '{}';

-- 5. Aliases: confidence columns (no-ops if present). Deliberately NO backfill here:
--    re-confirming every alias would defeat the "needs a second agreeing parse" rule.
alter table aliases add column if not exists confirmed boolean not null default false;
alter table aliases add column if not exists candidate_count integer not null default 1;

-- 6. Chat log (no-op if present)
create table if not exists chat_log (
  id         bigint generated always as identity primary key,
  role       text not null check (role in ('user', 'assistant')),
  content    text not null,
  created_at timestamptz not null default now()
);
alter table chat_log enable row level security;

-- 7. Indexes for the lookups the new code does (group delete, ordered pagination)
create index if not exists transactions_linked_tx_id_idx on transactions (linked_tx_id);
create index if not exists pool_contributions_linked_tx_id_idx on pool_contributions (linked_tx_id);
create index if not exists transactions_created_at_idx on transactions (created_at);
