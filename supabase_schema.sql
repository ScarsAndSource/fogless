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
