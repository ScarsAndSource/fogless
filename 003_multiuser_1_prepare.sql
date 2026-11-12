-- ============================================================================
-- fogless migration 003 (multi-user) -- STEP 1 of 2: PREPARE
-- Run in Supabase: Dashboard -> SQL Editor -> New query.   Safe to run again.
--
-- Adds a nullable user_id column to every table that holds a user's data. Nothing else changes, so
-- the app you have deployed today keeps working exactly as before while you roll out multi-user.
--
-- Order of operations for an EXISTING database:
--   1. Run this file (step 1).
--   2. Enable email sign-in in Supabase and deploy the multi-user code (see README).
--   3. Create YOUR account (sign up in the app, or Dashboard -> Authentication -> Users -> Add user).
--   4. Run 003_multiuser_2_finalize.sql (step 2), which assigns every existing row to you.
-- Between steps 2 and 4 the new app shows you an empty ledger; your data is not lost, it is just
-- not yet attached to your account.
-- ============================================================================
begin;

alter table transactions       add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table aliases            add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table settings           add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table pool_contributions add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table pool_expenses      add column if not exists user_id uuid references auth.users(id) on delete cascade;
alter table chat_log           add column if not exists user_id uuid references auth.users(id) on delete cascade;

commit;
