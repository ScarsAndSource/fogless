-- ============================================================================
-- Fogless -- complete schema for a NEW Supabase project (multi-user).
-- Run once: Dashboard -> SQL Editor -> New query.
--
-- Already running the single-user version? Do NOT run this. Run, in order:
--   003_multiuser_1_prepare.sql -> (deploy + create your account) -> 003_multiuser_2_finalize.sql -> 004_safety_and_speed.sql
--
-- Every table is owned per user (user_id -> auth.users). The server uses the service-role key and scopes
-- every query to the signed-in user; Row Level Security below is the safety net for anything that talks to
-- the database with a user's own token, and it blocks the public anon key entirely.
-- ============================================================================

create table if not exists transactions (
  id             bigint generated always as identity primary key,
  user_id        uuid not null default auth.uid() references auth.users(id) on delete cascade,
  type           text not null,
  amount         numeric not null,
  category       text,                       -- transfers have no spending category
  note           text,
  payment_method text,
  from_bucket    text,
  to_bucket      text,
  person         text,
  pool           text,
  linked_tx_id   bigint,
  created_at     timestamptz not null default now(),
  constraint transactions_type_check check (type in ('expense', 'income', 'transfer')),
  constraint transactions_amount_positive check (amount > 0)
);

create table if not exists aliases (
  user_id         uuid not null default auth.uid() references auth.users(id) on delete cascade,
  note_key        text not null,
  type            text not null check (type in ('expense', 'income')),
  category        text not null,
  payment_method  text,
  confirmed       boolean not null default false,
  candidate_count integer not null default 1,
  created_at      timestamptz not null default now(),
  primary key (user_id, note_key)
);

create table if not exists settings (
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  key     text not null,
  value   text,
  primary key (user_id, key)
);

create table if not exists pool_contributions (
  id           bigint generated always as identity primary key,
  user_id      uuid not null default auth.uid() references auth.users(id) on delete cascade,
  pool         text not null,
  person       text not null,
  amount       numeric not null,
  note         text,
  linked_tx_id bigint,
  created_at   timestamptz not null default now(),
  constraint pool_contributions_amount_positive check (amount > 0)
);

create table if not exists pool_expenses (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  pool       text not null,
  amount     numeric not null,
  category   text not null,
  note       text,
  created_at timestamptz not null default now(),
  constraint pool_expenses_amount_positive check (amount > 0)
);

create table if not exists chat_log (
  id         bigint generated always as identity primary key,
  user_id    uuid not null default auth.uid() references auth.users(id) on delete cascade,
  role       text not null check (role in ('user', 'assistant')),
  content    text not null,
  created_at timestamptz not null default now()
);

-- indexes (the app always filters by user_id first)
create index if not exists transactions_user_id_idx           on transactions (user_id, id);
create index if not exists transactions_user_created_idx      on transactions (user_id, created_at);
create index if not exists transactions_user_linked_idx       on transactions (user_id, linked_tx_id);
create index if not exists transactions_user_type_created_idx on transactions (user_id, type, created_at);
create index if not exists pool_contributions_user_pool_idx    on pool_contributions (user_id, pool);
create index if not exists pool_contributions_user_linked_idx  on pool_contributions (user_id, linked_tx_id);
create index if not exists pool_contributions_user_created_idx on pool_contributions (user_id, created_at);
create index if not exists pool_expenses_user_pool_idx         on pool_expenses (user_id, pool);
create index if not exists pool_expenses_user_created_idx      on pool_expenses (user_id, created_at);
create index if not exists chat_log_user_created_idx           on chat_log (user_id, created_at desc);

-- Row Level Security: signed-in users see only their own rows; anon sees nothing.
alter table transactions       enable row level security;
alter table aliases            enable row level security;
alter table settings           enable row level security;
alter table pool_contributions enable row level security;
alter table pool_expenses      enable row level security;
alter table chat_log           enable row level security;

drop policy if exists "own rows" on transactions;
create policy "own rows" on transactions       for all to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists "own rows" on aliases;
create policy "own rows" on aliases            for all to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists "own rows" on settings;
create policy "own rows" on settings           for all to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists "own rows" on pool_contributions;
create policy "own rows" on pool_contributions for all to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists "own rows" on pool_expenses;
create policy "own rows" on pool_expenses      for all to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));
drop policy if exists "own rows" on chat_log;
create policy "own rows" on chat_log           for all to authenticated
  using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()));

-- Per-user balances in one query (the server passes the signed-in user's id; only service_role may call it).
create or replace function bucket_balances(p_user_id uuid)
returns table(bucket text, total numeric)
language sql stable security invoker as $$
  select s.bucket, sum(s.delta) as total from (
    select coalesce(payment_method, 'cash') as bucket,
           case when type = 'income' then amount else -amount end as delta
      from transactions where user_id = p_user_id and type in ('income', 'expense')
    union all
    select coalesce(from_bucket, 'cash'), -amount from transactions where user_id = p_user_id and type = 'transfer'
    union all
    select coalesce(to_bucket, 'cash'), amount from transactions where user_id = p_user_id and type = 'transfer'
  ) s group by s.bucket
$$;
revoke all on function bucket_balances(uuid) from public, anon, authenticated;
grant execute on function bucket_balances(uuid) to service_role;
