create table if not exists public.web_scraper_leads (
  place_key text primary key,
  name text, category text, phone text, whatsapp_number text, whatsapp_source text,
  email text, emails text, website text,
  instagram text, facebook text, linkedin text, twitter_x text, youtube text, tiktok text,
  address text, country text, rating numeric, reviews_count int, price_range text,
  lat numeric, lng numeric, summary text, owner text, team text,
  maps_url text, place_id text, enrich_status text,
  scraped_at timestamptz, job_id int, job_query text, job_location text,
  synced_at timestamptz default now()
);
alter table public.web_scraper_leads enable row level security;
create policy "service role full access" on public.web_scraper_leads
  for all to service_role using (true) with check (true);
