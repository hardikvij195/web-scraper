-- 001_accounts.sql — idempotent
create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  role text not null check (role in ('admin','member')),
  name text,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.user_settings (
  user_id uuid primary key references auth.users(id) on delete cascade,
  webhook_url text,
  webhook_secret text,
  gemini_key text,
  openai_key text,
  updated_at timestamptz not null default now()
);

create table if not exists public.agent_tokens (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  token_hash text not null unique,
  label text,
  last_seen_at timestamptz,
  revoked boolean not null default false,
  created_at timestamptz not null default now()
);

-- security definer so RLS policies can call it without recursing into profiles' own policies
create or replace function public.is_admin() returns boolean
language sql stable security definer set search_path = public as
$$ select exists(select 1 from profiles p where p.user_id = auth.uid() and p.role = 'admin' and p.active) $$;

alter table public.profiles enable row level security;
alter table public.user_settings enable row level security;
alter table public.agent_tokens enable row level security;

drop policy if exists "own or admin read" on public.profiles;
create policy "own or admin read" on public.profiles for select
  using (user_id = auth.uid() or public.is_admin());

drop policy if exists "owner all" on public.user_settings;
create policy "owner all" on public.user_settings for all
  using (user_id = auth.uid()) with check (user_id = auth.uid());

drop policy if exists "own or admin read" on public.agent_tokens;
create policy "own or admin read" on public.agent_tokens for select
  using (user_id = auth.uid() or public.is_admin());
-- writes to all three go through the service-role API only (service role bypasses RLS)
