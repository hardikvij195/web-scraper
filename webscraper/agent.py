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


def _flat(r: dict) -> dict:
    from webscraper.supa import _row
    return _row(r)


def _local_progress(row: Any) -> dict:
    return {"scraped_count": row["scraped_count"], "links_found": row["links_found"],
            "enrich_done": row["enrich_done"], "enrich_total": row["enrich_total"]}


def run_agent(base: str, token: str, poll_sec: int = 20) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cloud = Cloud(base, token)
    if not srv.worker.is_alive():
        srv.worker.start()                   # same Worker the local UI uses
    store = Store()
    log.info("agent up — polling %s every %ss", base, poll_sec)
    while True:
        try:
            _tick(cloud, store)
        except httpx.HTTPError as e:
            log.warning("cloud unreachable: %s", e)
        time.sleep(poll_sec)


def _tick(cloud: Cloud, store: Store) -> None:
    mirrored = {r["cloud_id"] for r in store.conn.execute(
        "SELECT cloud_id FROM jobs WHERE cloud_id IS NOT NULL").fetchall()}
    for cj in cloud.jobs():
        if cj["id"] in mirrored:
            continue
        if cloud.claim(cj["id"]) is None:
            continue
        local_id = store.create_job(
            query=cj["query"], location=cj.get("location"),
            max_places=cj.get("limit_places") or 100, delay_sec=0,
            phase="queued", do_enrich=True, headless=True, country=cj.get("country"),
            radius_km=cj.get("radius_km"),
            center_lat=cj.get("lat"), center_lng=cj.get("lng"))
        store.update_job(local_id, cloud_id=cj["id"])
        log.info("cloud job #%s -> local job #%s", cj["id"], local_id)
    # mirror running/finished local state up
    for row in store.conn.execute(
            "SELECT * FROM jobs WHERE cloud_id IS NOT NULL AND (note IS NULL OR note <> 'synced')").fetchall():
        cid = row["cloud_id"]
        if row["phase"] in ("scraping", "enriching", "queued", "waiting", "researching"):
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
