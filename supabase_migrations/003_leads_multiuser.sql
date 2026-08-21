-- 003_leads_multiuser.sql — idempotent. Run AFTER scripts/create_admin.py has created the admin
-- (the backfill needs one profiles row with role='admin').
alter table public.web_scraper_leads add column if not exists user_id uuid references auth.users(id);
alter table public.web_scraper_leads add column if not exists cloud_job_id bigint;
alter table public.web_scraper_leads add column if not exists verified boolean not null default false;
alter table public.web_scraper_leads add column if not exists ai_summary text;
alter table public.web_scraper_leads add column if not exists webhook_status text;

update public.web_scraper_leads
  set user_id = (select user_id from public.profiles where role = 'admin' order by created_at limit 1)
  where user_id is null;
alter table public.web_scraper_leads alter column user_id set not null;

-- PK place_key → (user_id, place_key) so two members can own the same place
do $$ begin
  if exists (select 1 from information_schema.table_constraints
             where table_name = 'web_scraper_leads' and constraint_name = 'web_scraper_leads_pkey'
               and constraint_type = 'PRIMARY KEY')
     and not exists (select 1 from information_schema.constraint_column_usage
                     where constraint_name = 'web_scraper_leads_pkey' and column_name = 'user_id') then
    alter table public.web_scraper_leads drop constraint web_scraper_leads_pkey;
    alter table public.web_scraper_leads add primary key (user_id, place_key);
  end if;
end $$;

drop policy if exists "service role full access" on public.web_scraper_leads;
drop policy if exists "own or admin read" on public.web_scraper_leads;
create policy "own or admin read" on public.web_scraper_leads for select
  using (user_id = auth.uid() or public.is_admin());
