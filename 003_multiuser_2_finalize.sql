-- ============================================================================
-- fogless migration 003 (multi-user) -- STEP 2 of 2: FINALIZE
-- Run ONLY after step 1 and after your own account exists (see 003_multiuser_1_prepare.sql).
-- One transaction: if anything fails, NOTHING changes.  Safe to run again.
--
--  >>>  1. Put the email of YOUR account on the owner_email line below.  <<<
--
-- What it does:
--   * gives every existing (un-owned) row to that account,
--   * makes user_id mandatory, and part of the key for aliases and settings,
--   * adds per-user indexes,
--   * turns on Row Level Security with "you only see your own rows" policies. The Fogless server uses the
--     service-role key (which bypasses RLS) and scopes every query itself; these policies are the safety
--     net for anything that ever talks to the database with a user's own token.
-- ============================================================================
begin;

do $$
declare
  owner_email constant text := 'CHANGE-ME@example.com';   -- <<< your Fogless account email
  owner_id uuid;
begin
  if owner_email = 'CHANGE-ME@example.com' then
    raise exception 'Edit owner_email at the top of this script first.';
  end if;
  select id into owner_id from auth.users where lower(email) = lower(owner_email);
  if owner_id is null then
    raise exception 'No account with email % exists yet. Sign up in the app first, then re-run.', owner_email;
  end if;

  update transactions       set user_id = owner_id where user_id is null;
  update aliases            set user_id = owner_id where user_id is null;
  update settings           set user_id = owner_id where user_id is null;
  update pool_contributions set user_id = owner_id where user_id is null;
  update pool_expenses      set user_id = owner_id where user_id is null;
  update chat_log           set user_id = owner_id where user_id is null;
end $$;

-- user_id is now mandatory (and defaults to the caller for direct, token-authenticated inserts)
alter table transactions       alter column user_id set not null, alter column user_id set default auth.uid();
alter table aliases            alter column user_id set not null, alter column user_id set default auth.uid();
alter table settings           alter column user_id set not null, alter column user_id set default auth.uid();
alter table pool_contributions alter column user_id set not null, alter column user_id set default auth.uid();
alter table pool_expenses      alter column user_id set not null, alter column user_id set default auth.uid();
alter table chat_log           alter column user_id set not null, alter column user_id set default auth.uid();

-- Two users can each have an alias for "coffee" and a "starting_balance:cash" setting.
-- (If your original primary key had a non-default name this fails and rolls everything back; nothing is lost.)
alter table aliases  drop constraint if exists aliases_pkey;
alter table aliases  add primary key (user_id, note_key);
alter table settings drop constraint if exists settings_pkey;
alter table settings add primary key (user_id, key);

-- per-user indexes (the app always filters by user_id first)
create index if not exists transactions_user_id_idx          on transactions (user_id, id);
create index if not exists transactions_user_created_idx     on transactions (user_id, created_at);
create index if not exists transactions_user_linked_idx      on transactions (user_id, linked_tx_id);
create index if not exists transactions_user_type_created_idx on transactions (user_id, type, created_at);
create index if not exists pool_contributions_user_pool_idx  on pool_contributions (user_id, pool);
create index if not exists pool_contributions_user_linked_idx on pool_contributions (user_id, linked_tx_id);
create index if not exists pool_contributions_user_created_idx on pool_contributions (user_id, created_at);
create index if not exists pool_expenses_user_pool_idx       on pool_expenses (user_id, pool);
create index if not exists pool_expenses_user_created_idx    on pool_expenses (user_id, created_at);
create index if not exists chat_log_user_created_idx         on chat_log (user_id, created_at desc);

-- Row Level Security: a signed-in user can only ever touch rows where user_id is their own id.
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

commit;
