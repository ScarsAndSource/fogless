-- ============================================================================
-- fogless migration 005 -- lock-down + self-check.   Safe to run any time, as many times as you like.
-- Run in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
--
-- PART 1 (changes, all idempotent, none touches your data or the server's access):
--   * the public "anon" key gets NO privileges on the app tables. The Fogless server talks to the database
--     with the service_role key, so nothing in the app depends on anon access; this just removes a
--     second way in should the anon key ever leak.
--   * leftover tables from the removed Telegram bot (message_log, processed_updates), if you still have
--     them, get Row Level Security switched on with no policies = nobody but the service role can read them.
--     (Nothing is dropped. Drop them yourself once you are sure you do not need the old logs.)
-- PART 2: ONE result grid. Every row must say PASS. A FAIL row names exactly what to fix.
-- ============================================================================

-- ---------------------------------------------------------------- PART 1
revoke all on table transactions, aliases, settings, pool_contributions, pool_expenses, chat_log from anon;

do $$
begin
  if to_regclass('public.message_log') is not null then
    execute 'alter table public.message_log enable row level security';
    execute 'revoke all on table public.message_log from anon, authenticated';
  end if;
  if to_regclass('public.processed_updates') is not null then
    execute 'alter table public.processed_updates enable row level security';
    execute 'revoke all on table public.processed_updates from anon, authenticated';
  end if;
end $$;

-- ---------------------------------------------------------------- PART 2
with t(name) as (
  values ('transactions'), ('aliases'), ('settings'), ('pool_contributions'), ('pool_expenses'), ('chat_log')
)
select check_name, result from (
  select 'RLS is on: ' || t.name as check_name,
         case when coalesce((select c.relrowsecurity from pg_class c
                              where c.relname = t.name and c.relnamespace = 'public'::regnamespace), false)
              then 'PASS' else 'FAIL' end as result
    from t
  union all
  select 'no policy open to anon/public: ' || t.name,
         case when exists (select 1 from pg_policies p
                            where p.schemaname = 'public' and p.tablename = t.name
                              and p.roles && array['anon', 'public']::name[])
              then 'FAIL' else 'PASS' end
    from t
  union all
  select 'anon has no privileges on: ' || t.name,
         case when exists (select 1 from information_schema.role_table_grants g
                            where g.table_schema = 'public' and g.table_name = t.name and g.grantee = 'anon')
              then 'FAIL' else 'PASS' end
    from t
  union all
  select 'user_id is mandatory on: ' || t.name,
         case when exists (select 1 from information_schema.columns k
                            where k.table_schema = 'public' and k.table_name = t.name
                              and k.column_name = 'user_id' and k.is_nullable = 'NO')
              then 'PASS' else 'FAIL (run 003_multiuser_2_finalize.sql)' end
    from t
  union all
  select 'bucket_balances is not callable by anon/authenticated',
         case when exists (select 1 from pg_proc p
                            where p.proname = 'bucket_balances' and p.pronamespace = 'public'::regnamespace
                              and (has_function_privilege('anon', p.oid, 'execute')
                                   or has_function_privilege('authenticated', p.oid, 'execute')))
              then 'FAIL' else 'PASS' end
  union all
  select 'no old unscoped bucket_balances() exists',
         case when exists (select 1 from pg_proc p
                            where p.proname = 'bucket_balances' and p.pronamespace = 'public'::regnamespace
                              and p.pronargs = 0)
              then 'FAIL (run 004_safety_and_speed.sql)' else 'PASS' end
  union all
  select 'leftover table ' || x.name || ': absent or RLS on',
         case when to_regclass('public.' || x.name) is null then 'PASS'
              when (select c.relrowsecurity from pg_class c
                     where c.relname = x.name and c.relnamespace = 'public'::regnamespace) then 'PASS'
              else 'FAIL' end
    from (values ('message_log'), ('processed_updates')) as x(name)
) checks
order by (result <> 'PASS') desc, check_name;
