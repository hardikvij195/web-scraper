-- W11 (2026-08-30): the SaaS `web_scraper_leads` table was missing three columns the
-- agent's push payload has carried since W22/W26, so `supa.push_job` 400'd outright.
-- Types mirror the CRM's `lead_gen_results` (wa_verified text, enrich_error text,
-- wa_numbers jsonb). Idempotent — safe to re-run.
alter table public.web_scraper_leads add column if not exists wa_verified text;
alter table public.web_scraper_leads add column if not exists enrich_error text;
alter table public.web_scraper_leads add column if not exists wa_numbers jsonb;
