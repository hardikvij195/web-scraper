-- 002_jobs_credits.sql — idempotent
create table if not exists public.scrape_jobs (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  query text not null,
  location text,
  lat numeric, lng numeric, radius_km numeric,
  limit_places int not null default 100,
  country text,
  status text not null default 'queued'
    check (status in ('queued','claimed','running','paused_quota','done','error','cancelled')),
  phase text,
  progress jsonb not null default '{}'::jsonb,
  claimed_by bigint references public.agent_tokens(id),
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_scrape_jobs_user on public.scrape_jobs(user_id, status);

create table if not exists public.orders (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  pack text not null check (pack in ('starter_3k','pro_5k')),
  leads int not null,
  amount_inr int not null,             -- paise
  gateway text not null check (gateway in ('razorpay','payu')),
  gateway_order_id text unique,        -- razorpay order_id / payu txnid
  status text not null default 'created' check (status in ('created','paid','failed')),
  raw jsonb,
  created_at timestamptz not null default now(),
  paid_at timestamptz
);

create table if not exists public.credits_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  delta int not null,
  reason text not null check (reason in ('purchase','debit','admin_adjust')),
  order_id bigint references public.orders(id),
  job_id bigint references public.scrape_jobs(id),
  created_at timestamptz not null default now()
);
create index if not exists idx_ledger_user on public.credits_ledger(user_id);

create or replace view public.credit_balances as
  select user_id, coalesce(sum(delta), 0)::int as balance
  from public.credits_ledger group by user_id;

create table if not exists public.webhook_deliveries (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  lead_place_key text not null,
  url text not null,
  status text not null default 'pending' check (status in ('pending','ok','failed')),
  attempts int not null default 0,
  last_error text,
  next_retry_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_deliveries_pending on public.webhook_deliveries(status, next_retry_at);

-- Debits min(balance, requested); returns how many were actually debited. Race-safe via
-- per-user advisory lock. Called with service role only.
create or replace function public.debit_credits(p_user uuid, p_requested int, p_job bigint)
returns int language plpgsql security definer set search_path = public as $$
declare v_balance int; v_debit int;
begin
  if p_requested <= 0 then return 0; end if;
  perform pg_advisory_xact_lock(hashtext(p_user::text));
  select coalesce(sum(delta), 0) into v_balance from credits_ledger where user_id = p_user;
  v_debit := least(v_balance, p_requested);
  if v_debit > 0 then
    insert into credits_ledger(user_id, delta, reason, job_id) values (p_user, -v_debit, 'debit', p_job);
  end if;
  return v_debit;
end $$;

alter table public.scrape_jobs enable row level security;
alter table public.orders enable row level security;
alter table public.credits_ledger enable row level security;
alter table public.webhook_deliveries enable row level security;

drop policy if exists "own or admin read" on public.scrape_jobs;
create policy "own or admin read" on public.scrape_jobs for select
  using (user_id = auth.uid() or public.is_admin());
drop policy if exists "own or admin read" on public.orders;
create policy "own or admin read" on public.orders for select
  using (user_id = auth.uid() or public.is_admin());
drop policy if exists "own or admin read" on public.credits_ledger;
create policy "own or admin read" on public.credits_ledger for select
  using (user_id = auth.uid() or public.is_admin());
drop policy if exists "own or admin read" on public.webhook_deliveries;
create policy "own or admin read" on public.webhook_deliveries for select
  using (user_id = auth.uid() or public.is_admin());
