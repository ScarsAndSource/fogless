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

-- Row Level Security stays enabled with no policies, so only requests using the
-- service_role key (your bot's server-side key) can read/write. The public/anon
-- key -- which this bot never uses -- would be blocked entirely.
alter table transactions enable row level security;
alter table settings enable row level security;
