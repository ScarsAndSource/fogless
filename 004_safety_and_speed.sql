-- ============================================================================
-- fogless migration 004 -- run AFTER 003_multiuser_2_finalize.sql. Idempotent.
-- Optional but recommended: without the function the app still works, it just downloads the user's
-- whole ledger to compute balances instead of asking the database for the totals.
-- ============================================================================

-- 1. Per-user balances in ONE query. The user id is an ARGUMENT, so one account can only ever be
--    totalled over its own rows. Callable only with the service-role key (the Fogless server, which
--    always passes the signed-in user's id); a signed-in user's own token cannot execute it.
drop function if exists bucket_balances();   -- an earlier, unscoped draft of this function (sums EVERY user)
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

-- 2. Database-level guard: no zero/negative amounts. NOT VALID = existing rows are left alone (an old
--    split bug may have written 0.00 transfers); new and edited rows are checked. To clean up later:
--      delete from transactions where amount <= 0;
--      alter table transactions validate constraint transactions_amount_positive;
alter table transactions drop constraint if exists transactions_amount_positive;
alter table transactions add constraint transactions_amount_positive check (amount > 0) not valid;
alter table pool_contributions drop constraint if exists pool_contributions_amount_positive;
alter table pool_contributions add constraint pool_contributions_amount_positive check (amount > 0) not valid;
alter table pool_expenses drop constraint if exists pool_expenses_amount_positive;
alter table pool_expenses add constraint pool_expenses_amount_positive check (amount > 0) not valid;
