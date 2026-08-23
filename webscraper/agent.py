"""Agent mode: mirror cloud jobs into the local pipeline and report back.

Reuses the local Worker (webscraper/server.py) untouched: each cloud job becomes a local
jobs row (phase 'queued', cloud_id set); the Worker thread picks it up exactly as if the
local UI had created it. This loop watches local state and mirrors it up.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from webscraper import eta
from webscraper import server as srv
from webscraper.store import Store

log = logging.getLogger("webscraper.agent")


class Cloud:
    def __init__(self, base: str, token: str):
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=60,
                              headers={"X-Agent-Token": token, "Content-Type": "application/json"})

    def jobs(self) -> list[dict]:
        r = self.c.get("/api/agent/jobs")
        r.raise_for_status()
        return r.json()

    def claim(self, jid: int) -> dict | None:
        r = self.c.post(f"/api/agent/jobs/{jid}/claim")
        if r.status_code == 409:
            return None
        r.raise_for_status()
        return r.json()

    def progress(self, jid: int, phase: str | None, progress: dict) -> bool:
        """Returns True if the job was cancelled cloud-side."""
        r = self.c.post(f"/api/agent/jobs/{jid}/progress", json={"phase": phase, "progress": progress})
        r.raise_for_status()
        return bool(r.json().get("cancelled"))

    def done(self, jid: int, status: str, error: str | None = None) -> None:
        self.c.post(f"/api/agent/jobs/{jid}/done", json={"status": status, "error": error}).raise_for_status()

    def sync(self, jid: int, rows: list[dict]) -> dict:
        r = self.c.post("/api/agent/sync", json={"cloud_job_id": jid, "rows": rows})
        r.raise_for_status()
        return r.json()

    def logs(self, jid: int, rows: list[dict]) -> None:
        """Ship job_logs lines up. The SaaS API may not implement this yet — a 404 here is
        expected and handled by the caller, never fatal to the job."""
        self.c.post(f"/api/agent/jobs/{jid}/logs", json={"rows": rows}).raise_for_status()

    def config(self) -> dict:
        return {}  # SaaS members set AI keys in their cloud Settings tab, not here


def crm_payload(action: str, **kw) -> dict:
    return {"action": action, **kw}


class CrmCloud:
    """Same interface as Cloud, but speaks the CRM Edge Function protocol
    (single POST endpoint, {action: ...} bodies)."""

    def __init__(self, base: str, token: str):
        self.url = base.rstrip("/") + "/lead-finder-agent"
        self.c = httpx.Client(timeout=60, headers={"X-Agent-Token": token,
                                                   "Content-Type": "application/json"})

    def _post(self, payload: dict) -> httpx.Response:
        return self.c.post(self.url, json=payload)

    def jobs(self) -> list[dict]:
        r = self._post(crm_payload("jobs"))
        r.raise_for_status()
        return r.json()

    def claim(self, jid: int) -> dict | None:
        r = self._post(crm_payload("claim", job_id=jid))
        if r.status_code == 409:
            return None
        r.raise_for_status()
        return r.json()

    def progress(self, jid: int, phase: str | None, progress: dict) -> bool:
        r = self._post(crm_payload("progress", job_id=jid, phase=phase, progress=progress))
        r.raise_for_status()
        return bool(r.json().get("cancelled"))

    def done(self, jid: int, status: str, error: str | None = None) -> None:
        self._post(crm_payload("done", job_id=jid, status=status, error=error)).raise_for_status()

    def sync(self, jid: int, rows: list[dict]) -> dict:
        r = self._post(crm_payload("sync", job_id=jid, rows=rows))
        r.raise_for_status()
        # CRM has no quota — normalize to the Cloud.sync result shape _tick expects
        d = r.json()
        return {"accepted": d.get("accepted", 0), "rejected_quota": 0}

    def logs(self, jid: int, rows: list[dict]) -> None:
        """Append this job's new log lines to the CRM (`lead_gen_job_logs`).

        Deliberately NOT folded into `progress`: progress is overwritten every tick, the
        log is append-only, and a rejected log batch must be retried without also
        re-sending (or losing) a progress update. The CRM side of this action may not be
        deployed yet — the caller treats any failure as "retry next tick".
        """
        self._post(crm_payload("logs", job_id=jid, rows=rows)).raise_for_status()

    def config(self) -> dict:
        """API keys set on the CRM Lead Finder Setup tab (gemini_api_key, ...)."""
        r = self._post(crm_payload("config"))
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, dict) else {}

    def results(self, jid: int) -> list[dict]:
        """Existing results of a job (for on-demand WhatsApp re-verify)."""
        r = self._post(crm_payload("results", job_id=jid))
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else []

    def set_wa(self, jid: int, updates: list[dict]) -> None:
        self._post(crm_payload("set_wa", job_id=jid, updates=updates)).raise_for_status()


def _flat(r: dict) -> dict:
    from webscraper.supa import _row
    return _row(r)


#: Log lines shipped per tick. Bounded so a job that logged for an hour while the CRM was
#: unreachable cannot post a multi-megabyte body when it comes back; the rest goes next tick.
LOG_BATCH = 200


def _local_progress(row: Any, store: Store | None = None) -> dict:
    """Counters the CRM's progress bars read, plus the phase/lane/ETA block (T136 W2+W4).

    The ETA is computed here rather than CRM-side on purpose: only this machine knows
    how fast it actually scrapes, and `eta.summarise` reads the rolling per-phase
    averages out of the local SQLite. The CRM just renders what it is told, and shows
    "estimating…" wherever `eta_sec` is null.

    `lanes` is the truthful picture since the 2026-08-23 rewrite — three concurrent lanes,
    each with its own runtime and end reason. It rides inside the same `progress` jsonb, so
    the CRM needs no schema change; `phases` stays alongside it for the strip the CRM
    already renders, and an older CRM that ignores `lanes` keeps working unchanged.
    """
    out = {"scraped_count": row["scraped_count"], "links_found": row["links_found"],
           "enrich_done": row["enrich_done"], "enrich_total": row["enrich_total"],
           "research_done": row["research_done"], "research_total": row["research_total"],
           "wa_verify_done": row["wa_verify_done"], "wa_verify_total": row["wa_verify_total"]}
    s = eta.summarise(row, store)
    out.update({"phases": s["phases"], "lanes": s["lanes"], "eta_sec": s["eta_sec"],
                "phase_eta_sec": s["phase_eta_sec"], "estimating": s["estimating"],
                "budget_left_sec": (round(s["budget_left_sec"])
                                    if s["budget_left_sec"] is not None else None)})
    return out


def _col(row: Any, key: str, default: Any = None) -> Any:
    """Read a column that may not exist yet (sqlite3.Row raises IndexError, dict KeyError)."""
    try:
        v = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def _ship_logs(cloud: "Cloud | CrmCloud", store: Store, row: Any) -> None:
    """Send this job's unsent `job_logs` lines up, then move the watermark.

    Order matters: `jobs.logs_synced_upto` advances ONLY after the POST returned 2xx, so a
    failed tick re-sends the same lines next time instead of dropping them silently. The
    id of the last row in the batch is the watermark (ids are monotonic per SQLite
    AUTOINCREMENT), which also means a partially-consumed batch is simply resent — the CRM
    upserts, so a duplicate line is harmless where a lost one is not.

    Failure is never fatal: the CRM's `logs` action may not be deployed yet (404), or the
    network may be down. A job must not die because its diary could not be filed.
    """
    cid = _col(row, "cloud_id")
    if not cid:
        return
    job_id = int(row["id"])
    rows = store.logs(job_id, after_id=int(_col(row, "logs_synced_upto", 0) or 0),
                      limit=LOG_BATCH)
    if not rows:
        return
    try:
        cloud.logs(int(cid), [{"ts": r["ts"], "lane": r["lane"], "level": r["level"],
                               "message": r["message"]} for r in rows])
    except Exception as e:                                       # noqa: BLE001
        # Broad on purpose — see the docstring. httpx raises HTTPError, but a malformed
        # response body or a JSON error would raise something else entirely.
        log.warning("log ship to #%s failed (%d line(s) held for the next tick): %s",
                    cid, len(rows), e)
        return
    store.update_job(job_id, logs_synced_upto=int(rows[-1]["id"]))


def _requeue_orphans(store: Store, kind: str) -> int:
    """Put jobs the last agent died mid-run back on the Worker's queue.

    The Worker only picks up `phase IN ('queued','waiting')`, so a job killed
    while scraping stays on 'scraping' for ever: nothing restarts it, `_tick`
    keeps mirroring that phase up, and because those progress pings refresh
    `updated_at` the CRM's 30-minute stale-reclaim never opens either. The job
    reads "running" in the UI and does nothing at all.

    Re-running is safe: places upsert on (job_id, place_key) locally and the CRM
    upserts on the same pair, so a resumed job overwrites its own rows instead of
    duplicating them. Jobs already synced or explicitly stopped are left alone.
    """
    rows = store.conn.execute(
        "SELECT id FROM jobs WHERE cloud_id IS NOT NULL AND cloud_kind=? "
        "AND phase IN ('scraping','enriching','researching','verifying_wa') "
        "AND (note IS NULL OR note <> 'synced') "
        "AND COALESCE(stop_requested,0)=0", (kind,)).fetchall()
    for r in rows:
        store.update_job(int(r["id"]), phase="queued",
                         message="resuming after agent restart")
    return len(rows)


def run_agent(base: str, token: str, poll_sec: int = 20, kind: str = "saas") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cloud = CrmCloud(base, token) if kind == "crm" else Cloud(base, token)
    if not srv.worker.is_alive():
        srv.worker.start()                   # same Worker the local UI uses
    store = Store()
    orphans = _requeue_orphans(store, kind)
    if orphans:
        log.info("requeued %d job(s) left mid-run by a previous agent", orphans)
    # Pull API keys from the cloud/CRM (Setup tab) — local .env always wins.
    import os
    try:
        for k, v in cloud.config().items():
            env = k.upper()  # gemini_api_key -> GEMINI_API_KEY
            if v and not os.getenv(env):
                os.environ[env] = v
                log.info("config: %s set from cloud", env)
    except httpx.HTTPError as e:
        log.warning("config fetch failed (continuing with local .env): %s", e)
    log.info("agent up (%s) — polling %s every %ss", kind, base, poll_sec)
    # local job id -> highest places.rowid already streamed up. In memory only: after a
    # restart it starts at 0 and re-sends that job's rows once, which upsert absorbs.
    synced_upto: dict[int, int] = {}
    while True:
        try:
            _tick(cloud, store, kind, synced_upto)
        except httpx.HTTPError as e:
            log.warning("cloud unreachable: %s", e)
        time.sleep(poll_sec)


def _reverify_wa(cloud: "CrmCloud", store: Store, jid: int) -> None:
    """Verify an existing CRM job's numbers on WhatsApp and sync wa_verified back.
    No scraping/enriching — operates purely on the job's already-synced results."""
    from webscraper import wa_verify
    try:
        rows = cloud.results(jid)
    except httpx.HTTPError as e:
        log.warning("re-verify #%s: could not fetch results: %s", jid, e)
        cloud.done(jid, "error", "could not fetch results")
        return
    # Only rows with a number AND not already decided — a 'yes'/'no' is final, re-checking
    # it wastes the daily cap. 'unknown'/null are re-tried.
    targets = [r for r in rows if (r.get("phone") or r.get("whatsapp_number"))
               and r.get("wa_verified") not in ("yes", "no")]
    already = sum(1 for r in rows if r.get("wa_verified") in ("yes", "no"))
    total = len(targets)
    src_by_pk = {r["place_key"]: r.get("whatsapp_source") for r in targets}
    if already:
        log.info("re-verify #%s: skipping %d already-verified", jid, already)

    # A re-verify has exactly one phase, so it gets its ETA straight from the rolling
    # WhatsApp-check rate instead of the full job model. `None` (no history yet) is
    # passed through untouched — the CRM renders "estimating…" for it.
    started = time.monotonic()

    def wa_progress(done: int) -> dict:
        per = ((time.monotonic() - started) / done) if done >= 3 else store.phase_rate("verifying_wa")
        eta_sec = round(max(0, total - done) * per) if per else None
        src = "live" if done >= 3 else "history"
        # A re-verify runs exactly one lane, so `lanes` is hand-built here rather than read
        # off a jobs row — there is no local job row for it (the work is driven straight off
        # the CRM's existing results). Same shape as eta.lanes() so the CRM renders it with
        # the same code path; the two idle lanes are reported 'disabled', not 'pending'.
        wa_lane = {"key": "whatsapp", "label": eta.LANE_LABELS["whatsapp"],
                   "unit": eta.LANE_UNITS["whatsapp"], "status": "running",
                   "done": done, "total": total, "total_is_min": False,
                   "eta_sec": eta_sec, "estimating": eta_sec is None, "rate_source": src,
                   "started_at": None, "ended_at": None, "ok": None, "reason": None,
                   "ran_sec": round(time.monotonic() - started)}
        idle = [{"key": k, "label": eta.LANE_LABELS[k], "unit": eta.LANE_UNITS[k],
                 "status": "disabled", "done": 0, "total": None, "total_is_min": False,
                 "eta_sec": None, "estimating": False, "rate_source": "done",
                 "started_at": None, "ended_at": None, "ok": None, "reason": "disabled",
                 "ran_sec": None} for k in ("discovery", "enrichment")]
        return {"wa_verify_total": total, "wa_verify_done": done,
                "eta_sec": eta_sec, "phase_eta_sec": eta_sec, "estimating": eta_sec is None,
                "lanes": idle + [wa_lane],
                "phases": [{"key": "verifying_wa", "label": eta.LABELS["verifying_wa"],
                            "unit": eta.UNITS["verifying_wa"], "status": "running",
                            "done": done, "total": total, "total_is_min": False,
                            "eta_sec": eta_sec, "lane": "whatsapp",
                            "estimating": eta_sec is None, "rate_source": src}]}

    cloud.progress(jid, "verifying_wa", wa_progress(0))
    collected: dict[str, str] = {}

    def onp(pk: str, status: str, num: str | None = None) -> None:
        collected[pk] = status
        upd: dict[str, Any] = {"place_key": pk, "wa_verified": status}
        # Confirmed hit -> promote the verified number + mark source 'verified' (drops an
        # 'unverified' guess). Miss on a guessed number -> clear it.
        # 'assumed_mobile' is the retired spelling of 'unverified' (2026-08-23); still
        # accepted here so rows written before the migration still clear correctly.
        if status == "yes" and num:
            upd["whatsapp_number"] = num
            upd["whatsapp_source"] = "verified"
        elif status == "no" and src_by_pk.get(pk) in ("unverified", "assumed_mobile"):
            upd["whatsapp_number"] = None
            upd["whatsapp_source"] = None
        # Flush after EVERY check so the CRM's progress bar + ✓/✗ badges move live.
        try:
            cloud.progress(jid, "verifying_wa", wa_progress(len(collected)))
            cloud.set_wa(jid, [upd])
        except httpx.HTTPError as e:
            log.warning("re-verify #%s: progress/set_wa failed: %s", jid, e)

    try:
        # job_id=None → verify_places won't touch the local store's places (they aren't
        # here); `store` is still used for WA account rotation + daily caps.
        wa_verify.verify_places(store, targets, on_progress=onp, job_id=None)
    except wa_verify.WaNotLoggedIn as e:
        cloud.done(jid, "error", f"WhatsApp verify skipped — {e}")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("re-verify #%s failed", jid)
        cloud.done(jid, "error", str(e)[:200])
        return
    # Feed the rolling average so the next verify run can be estimated (W4).
    store.record_phase_rate(None, "verifying_wa", len(collected), time.monotonic() - started)
    if collected:
        cloud.set_wa(jid, [{"place_key": k, "wa_verified": v} for k, v in collected.items()])
    cloud.done(jid, "done")
    log.info("re-verify #%s: %d checked", jid, len(collected))


def _tick(cloud: "Cloud | CrmCloud", store: Store, kind: str = "saas",
          synced_upto: dict[int, int] | None = None) -> None:
    mirrored = {r["cloud_id"] for r in store.conn.execute(
        "SELECT cloud_id FROM jobs WHERE cloud_id IS NOT NULL AND cloud_kind=?", (kind,)).fetchall()}
    for cj in cloud.jobs():
        # On-demand WhatsApp re-verify: no scrape/enrich — fetch the job's existing
        # results, check each number, write wa_verified back. CRM-only. Checked BEFORE
        # the `mirrored` guard because a re-verify targets a job that was ALREADY
        # scraped+mirrored earlier — the guard would otherwise skip it forever.
        if kind == "crm" and cj.get("wa_verify_only"):
            if cloud.claim(cj["id"]) is None:
                continue
            _reverify_wa(cloud, store, cj["id"])
            continue
        if cj["id"] in mirrored:
            continue
        if cloud.claim(cj["id"]) is None:
            continue
        limit = cj.get("limit_places")
        local_id = store.create_job(
            query=cj["query"], location=cj.get("location"),
            max_places=100 if limit is None else int(limit),   # 0 = unlimited
            delay_sec=float(cj.get("delay_sec") or 0),
            phase="queued",
            do_enrich=bool(cj.get("do_enrich", True)),
            headless=bool(cj.get("headless", True)),
            country=cj.get("country"),
            radius_km=cj.get("radius_km"),
            center_lat=cj.get("lat"), center_lng=cj.get("lng"),
            max_minutes=cj.get("max_minutes"),
            unique_new=bool(cj.get("unique_new", False)))
        import json as _json
        locs = cj.get("locations")
        store.update_job(local_id, cloud_id=cj["id"], cloud_kind=kind,
                         do_research=int(bool(cj.get("do_research", False))),
                         do_wa_verify=int(bool(cj.get("do_wa_verify", False))),
                         locations=_json.dumps(locs) if isinstance(locs, list) and len(locs) > 1 else None)
        log.info("cloud job #%s -> local job #%s", cj["id"], local_id)
    # mirror running/finished local state up
    for row in store.conn.execute(
            "SELECT * FROM jobs WHERE cloud_id IS NOT NULL AND cloud_kind=? "
            "AND (note IS NULL OR note <> 'synced')", (kind,)).fetchall():
        cid = row["cloud_id"]
        # Diary first, in BOTH branches: a job that finished between two ticks still has
        # its closing lines (lane reasons, the failure text) waiting to be shipped, and
        # once the terminal branch marks it 'synced' this loop never looks at it again.
        _ship_logs(cloud, store, row)
        if row["phase"] in ("scraping", "enriching", "queued", "waiting", "researching", "verifying_wa"):
            # Stream leads up AS THEY LAND. Results used to be uploaded only once the job
            # reached a terminal phase, so a job stopped by the user or by its time limit
            # showed 0 leads in the CRM even though places had been scraped. Rows are
            # upserted on (job_id, place_key), so the full sync at the end still overwrites
            # these with their enriched/researched versions.
            if synced_upto is not None:
                fresh, top = store.places_after(row["id"], synced_upto.get(row["id"], 0))
                if fresh:
                    try:
                        for i in range(0, len(fresh), 200):
                            cloud.sync(cid, [_flat(f) for f in fresh[i:i + 200]])
                        synced_upto[row["id"]] = top
                        log.info("streamed %d new lead(s) to job #%s", len(fresh), cid)
                    except httpx.HTTPError as e:
                        log.warning("stream to #%s failed (will retry next tick): %s", cid, e)
            cancelled = cloud.progress(cid, row["phase"], _local_progress(row, store))
            if cancelled:
                store.update_job(row["id"], stop_requested=1, note="synced")
        elif row["phase"] in ("done", "stopped", "failed"):
            rows = store.places(row["id"])
            quota_hit = False
            for i in range(0, len(rows), 200):
                res = cloud.sync(cid, [_flat(r) for r in rows[i:i + 200]])
                log.info("sync job #%s: accepted %s, rejected_quota %s",
                         cid, res.get("accepted"), res.get("rejected_quota"))
                if res.get("rejected_quota"):
                    quota_hit = True
                    break            # out of credits — retried on a later tick after top-up
            if not quota_hit:
                # Send the real failure text, not the phase name. This used to pass
                # row["phase"], so every failure reached the CRM as the literal string
                # "failed" and the only way to find out what actually broke was to open
                # data/agent.log on this PC. The Worker already stores the exception in
                # jobs.message; fall back to the phase only when there is none.
                failure = (row["message"] or "").strip() or f"failed during {row['phase']}"
                cloud.done(cid, "done" if row["phase"] == "done" else "error",
                           None if row["phase"] == "done" else failure[:300])
                store.update_job(row["id"], note="synced")
