-- fogless migration 003 -- run ONCE in Supabase: Dashboard -> SQL Editor -> New query. Idempotent.
-- Run it BEFORE (or right after) deploying the patched api/index.py; without it the app still works,
-- it just falls back to downloading every row to compute balances.

-- 1. Balances in ONE query instead of downloading the whole ledger on every message.
create or replace function bucket_balances()
returns table(bucket text, total numeric)
language sql stable security invoker as $$
  select bucket, sum(delta) as total from (
    select coalesce(payment_method, 'cash') as bucket,
           case when type = 'income' then amount else -amount end as delta
      from transactions where type in ('income', 'expense')
    union all
    select coalesce(from_bucket, 'cash'), -amount from transactions where type = 'transfer'
    union all
    select coalesce(to_bucket, 'cash'), amount from transactions where type = 'transfer'
  ) s group by bucket
$$;
revoke all on function bucket_balances() from public, anon, authenticated;
grant execute on function bucket_balances() to service_role;

-- 2. Database-level guard: no zero/negative amounts (NOT VALID = old rows are left alone; the old split
--    bug may have written 0.00 transfers. Clean them with: delete from transactions where amount <= 0;
--    then: alter table transactions validate constraint transactions_amount_positive;)
alter table transactions drop constraint if exists transactions_amount_positive;
alter table transactions add constraint transactions_amount_positive check (amount > 0) not valid;
alter table pool_contributions drop constraint if exists pool_contributions_amount_positive;
alter table pool_contributions add constraint pool_contributions_amount_positive check (amount > 0) not valid;
alter table pool_expenses drop constraint if exists pool_expenses_amount_positive;
alter table pool_expenses add constraint pool_expenses_amount_positive check (amount > 0) not valid;

-- 3. Undo / history lookups
create index if not exists pool_contributions_created_at_idx on pool_contributions (created_at);
create index if not exists pool_expenses_created_at_idx on pool_expenses (created_at);
create index if not exists transactions_type_created_idx on transactions (type, created_at);
