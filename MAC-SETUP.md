# Lead Finder agent on a Mac — setup + multi-device test  (written 2026-08-25)

Read this first in the Mac session. The Mac needs ONLY this repo (`web-scraper`) — the CRM
(`hvt-ai-crm-live`) is served from Vercel and does not have to run locally.

## How device targeting works (state as pushed 2026-08-25)

- Every agent call to the CRM Edge Function `lead-finder-agent` carries `device` =
  `LEAD_FINDER_DEVICE` env var, else the hostname (`webscraper/agent.py:_device_name`).
- The Edge Function upserts `(token_id, device, last_seen_at)` into `public.lead_gen_agents`
  on every call (heartbeat). A device counts as **online** when seen in the last 2 min.
- `lead_gen_jobs.target_agent` (text, null = any agent). `jobs` / `claim` only offer or
  hand out a pinned job to the agent whose `device` matches; an agent that sends no
  `device` only sees untargeted jobs.
- CRM UI: Lead Finder → New job → **Run on** dropdown (`LeadGenJobModal.tsx`), listed
  from `useLeadGenAgents()` (polls every 20 s). Shows once ≥ 1 device has ever checked in
  (commit `d138a1d`); the last choice is remembered per browser in
  `localStorage.lead_finder_target_agent`. Job cards show `· on <device>`.
- Migration `supabase/migrations/20260824T2100_lead_finder_device_targeting.sql` and the
  Edge Function are **already applied/deployed on prod** (`fyfhkjxewzbyxdwspkuc`) — verified
  2026-08-25 (`HARDIK-PC` row present). Nothing DB-side to do on the Mac.

## Mac setup (one time)

```bash
# 1. prerequisites
brew install python@3.13 git            # Google Chrome installed normally (real Chrome preferred by browser_fetch.py)
git clone https://github.com/hardikvij195/web-scraper.git && cd web-scraper

# 2. deps
python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium     # fallback when Chrome is absent

# 3. .env  (copy .env.example, then set)
#    CRM_AGENT_TOKEN=wsk_…          <- mint a NEW token: CRM → Lead Finder → Setup tab → Agent tokens → New token
#                                      (tokens are hash-only; the PC's token cannot be recovered — one per machine)
#    LEAD_FINDER_DEVICE=Hardik-MacBook   <- optional friendly name; default = computer name
#    (Gemini/Groq keys are NOT needed here — the CRM's `config` action pushes them to the agent)

# 4. run once by hand to see it register
bash run-agent-loop.sh &   # logs to data/agent.log; Ctrl-C / kill when done
tail -f data/agent.log     # expect "starting agent" then polling lines, no 401

# 5. start at login + keep alive (launchd)
bash scripts/install-agent-autostart-mac.sh
#    uninstall: launchctl bootout gui/$(id -u)/app.hvtechnologies.leadfinder-agent
#               rm ~/Library/LaunchAgents/app.hvtechnologies.leadfinder-agent.plist
```

WhatsApp verification lane needs a WA Web session ON THE MACHINE THAT RUNS THE JOB:
`.venv/bin/python -m webscraper wa-login <label>` once on the Mac (QR scan). Without it the
WhatsApp lane ends `wa_not_logged_in`; discovery + enrichment still run.

## Verify registration

- CRM → Lead Finder → New job → **Run on** lists `Hardik-MacBook · online` next to `HARDIK-PC`.
- Or REST (service role, from the CRM repo `.env`):
  `GET {VITE_SUPABASE_URL}/rest/v1/lead_gen_agents?select=device,last_seen_at`
- Windows PC side: agent runs as scheduled task "HVT Lead Finder Agent" (`run-agent-loop.bat`),
  log `data\agent.log`.

## Multi-device test plan

| # | Do | Expect |
|---|----|--------|
| 1 | Both agents running. New job, Run on = `Hardik-MacBook` | Headed Chrome opens on the **Mac** only; PC's `agent.log` never claims it; card shows `· on Hardik-MacBook` |
| 2 | New job, Run on = `HARDIK-PC` while sitting at the Mac | Runs on the PC; nothing opens on the Mac |
| 3 | Run on = **Any online machine** | Whichever polls first claims it (old behaviour) |
| 4 | Pin to the Mac, then quit the Mac agent | Job stays `queued` ("make sure that machine's agent is running" toast); resumes when the Mac agent returns; PC never steals it |
| 5 | Close the headed Chrome window twice mid-discovery | Lane relaunches (up to 3×, `browser_restart` events) instead of ending the job (fix `maps.py` 2026-08-25) |
| 6 | Job whose discovery finds nothing | Enrichment + WhatsApp cards read **No leads**, not "Completed" |

Known limits: a device that has never run the agent is not pickable (no free-text entry);
a pinned job to an offline device waits forever until Stop; `lead_gen_agents` is never
pruned (stale devices show `· offline`).

## Related docs
- `README.md` "Run the CRM Lead Finder agent on a Mac"; `CLAUDE.md` (env table, W15 proxies).
- `tasks.md` W15/W16; CRM `hvt-ai-crm-live/tasks.md` T163–T167.
