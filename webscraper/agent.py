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


def _local_progress(row: Any) -> dict:
    return {"scraped_count": row["scraped_count"], "links_found": row["links_found"],
            "enrich_done": row["enrich_done"], "enrich_total": row["enrich_total"],
            "research_done": row["research_done"], "research_total": row["research_total"],
            "wa_verify_done": row["wa_verify_done"], "wa_verify_total": row["wa_verify_total"]}


def run_agent(base: str, token: str, poll_sec: int = 20, kind: str = "saas") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cloud = CrmCloud(base, token) if kind == "crm" else Cloud(base, token)
    if not srv.worker.is_alive():
        srv.worker.start()                   # same Worker the local UI uses
    store = Store()
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
    while True:
        try:
            _tick(cloud, store, kind)
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
    cloud.progress(jid, "verifying_wa", {"wa_verify_total": total, "wa_verify_done": 0})
    collected: dict[str, str] = {}

    def onp(pk: str, status: str, num: str | None = None) -> None:
        collected[pk] = status
        upd: dict[str, Any] = {"place_key": pk, "wa_verified": status}
        # Confirmed hit -> promote the verified number + mark source 'verified' (drops an
        # 'assumed_mobile' guess). Miss on a guessed number -> clear it.
        if status == "yes" and num:
            upd["whatsapp_number"] = num
            upd["whatsapp_source"] = "verified"
        elif status == "no" and src_by_pk.get(pk) == "assumed_mobile":
            upd["whatsapp_number"] = None
            upd["whatsapp_source"] = None
        # Flush after EVERY check so the CRM's progress bar + ✓/✗ badges move live.
        try:
            cloud.progress(jid, "verifying_wa",
                           {"wa_verify_total": total, "wa_verify_done": len(collected)})
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
    if collected:
        cloud.set_wa(jid, [{"place_key": k, "wa_verified": v} for k, v in collected.items()])
    cloud.done(jid, "done")
    log.info("re-verify #%s: %d checked", jid, len(collected))


def _tick(cloud: "Cloud | CrmCloud", store: Store, kind: str = "saas") -> None:
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
        if row["phase"] in ("scraping", "enriching", "queued", "waiting", "researching", "verifying_wa"):
            cancelled = cloud.progress(cid, row["phase"], _local_progress(row))
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
                cloud.done(cid, "done" if row["phase"] == "done" else "error",
                           None if row["phase"] == "done" else row["phase"])
                store.update_job(row["id"], note="synced")
