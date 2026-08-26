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

from webscraper.config import settings
from datetime import datetime, timezone

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


def _device_name_path():
    from webscraper.config import ROOT
    return ROOT / "data" / "device_name"


def _remember_device_name(name: str) -> None:
    """Persist the label so it survives reboots — see _device_name."""
    try:
        p = _device_name_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists() or p.read_text(encoding="utf-8").strip() != name:
            p.write_text(name + "\n", encoding="utf-8")
    except OSError:
        pass


def _device_name() -> str:
    """This machine's label, sent on every CRM call so the job can be pinned to it.

    Order: `data/device_name` (what this machine called itself last time, incl. a rename
    from the CRM) → `LEAD_FINDER_DEVICE` (installer --device) → macOS ComputerName →
    hostname. The winner is written back to `data/device_name` so it never changes again:
    under launchd a Mac's hostname came back as `Unknown_26:e5:…` and then
    `Unknown_ba:67:…` on the next boot, so every restart registered a NEW device and
    orphaned the jobs pinned to the old one (2026-08-26)."""
    import os
    import socket
    import subprocess
    name = ""
    try:
        name = _device_name_path().read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if not name:
        name = (os.getenv("LEAD_FINDER_DEVICE") or "").strip()
    if not name:
        for args in (["scutil", "--get", "ComputerName"], ["scutil", "--get", "LocalHostName"]):
            try:
                out = subprocess.run(args, capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                out = ""
            if out and not out.startswith("Unknown"):
                name = out
                break
    if not name:
        name = socket.gethostname() or "agent"
    name = name.strip()[:120]
    _remember_device_name(name)
    return name


#: Computed once — the hostname does not change while the agent runs.
DEVICE_NAME = _device_name()


def crm_payload(action: str, **kw) -> dict:
    # `device` rides on every call: it is both the heartbeat that puts this machine in the
    # CRM's "Run on" list and the key the CRM uses to route a targeted job here.
    return {"action": action, "device": DEVICE_NAME, **kw}


class CrmCloud:
    """Same interface as Cloud, but speaks the CRM Edge Function protocol
    (single POST endpoint, {action: ...} bodies)."""

    def __init__(self, base: str, token: str):
        self.url = base.rstrip("/") + "/lead-finder-agent"
        self.c = httpx.Client(timeout=60, headers={"X-Agent-Token": token,
                                                   "Content-Type": "application/json"})

    def _post(self, payload: dict) -> httpx.Response:
        return self.c.post(self.url, json=payload)

    #: Self-check cadence. The checks are cheap but there is no point re-sending an
    #: unchanged picture every second while a job is busy.
    CHECKS_EVERY_SEC = 300.0
    _checks_sent_at = 0.0

    def jobs(self) -> list[dict]:
        extra: dict = {}
        if time.monotonic() - self._checks_sent_at >= self.CHECKS_EVERY_SEC:
            try:
                from .healthcheck import run_checks
                extra["checks"] = run_checks()
            except Exception:                                     # noqa: BLE001
                log.debug("self-check failed", exc_info=True)
            self._checks_sent_at = time.monotonic()
        r = self._post(crm_payload("jobs", **extra))
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

    def agent_logs(self, rows: list[dict]) -> None:
        """Ship device-level log lines (T208). A 404 from an older Edge Fn is fine."""
        self._post(crm_payload("agent_logs", rows=rows)).raise_for_status()

    def command(self) -> dict | None:
        """Pop the one out-of-band command the CRM may have left for THIS device
        (T202: `wa_login`). None when there is nothing, or on an Edge Fn that predates it."""
        r = self._post(crm_payload("command"))
        if r.status_code >= 400:
            return None
        d = r.json()
        return d if isinstance(d, dict) and d.get("command") else None

    def command_done(self, cid: int, ok: bool, result: str | None = None) -> None:
        self._post(crm_payload("command_done", id=cid, status="done" if ok else "failed",
                               result=result)).raise_for_status()

    def results(self, jid: int) -> list[dict]:
        """Existing results of a job (for on-demand WhatsApp re-verify)."""
        r = self._post(crm_payload("results", job_id=jid))
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else []

    def set_wa(self, jid: int, updates: list[dict]) -> None:
        self._post(crm_payload("set_wa", job_id=jid, updates=updates)).raise_for_status()


def _flat(r: dict) -> dict:
    """One CRM `sync` row.

    Keys whose value is None are DROPPED. Since 2026-08-23 the Edge Function treats sync
    as a partial patch — absent key = leave the stored value alone, present-and-null =
    deliberately clear it — so emitting every column unconditionally (what `_row` does for
    the SaaS table) would wipe good CRM data with nulls the local row simply does not
    carry. That is not theoretical: `wa_verified` and `whatsapp_source` are both in the
    function's column list, so a re-enrich batch would have erased WhatsApp verification
    results for every lead it touched.

    Empty strings go too, for the same reason one step subtler: `fmt_team(None)` returns
    `""`, not None, so `team` was present in EVERY row and would have overwritten a real
    team list with blank on any partial sync.

    Nothing is lost by dropping them: a brand-new lead's omitted columns take their column
    defaults, and the only path that must genuinely clear a value — a WhatsApp miss — goes
    through the `set_wa` action, never through `sync`.
    """
    from webscraper.supa import _row
    return {k: v for k, v in _row(r).items() if v is not None and v != ""}


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
    # Discovery stats for the card's info dialog (T172): what Maps offered, what was
    # opened, what was skipped and why. Best-effort — an old store has no job_links.
    try:
        offered, opened = store.link_counts(row["id"]) if store is not None else (0, 0)
        out.update({"links_offered": offered, "links_opened": opened,
                    "skipped_known": int(_col(row, "skipped_known", 0) or 0),
                    "skipped_far": int(_col(row, "skipped_far", 0) or 0)})
    except Exception:                                             # noqa: BLE001
        pass
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


#: How long the loop waits after a tick that DID something. A user who just pressed
#: "Re-verify" is watching the screen, so the next look must be almost immediate —
#: re-runs used to sit at "queued" for up to a full poll interval (15s in the
#: scheduled task) before anything visibly happened.
BUSY_POLL_SEC = 1.0

#: After work stops, stay in the fast lane this long before going back to the
#: configured interval. Covers the common "finish one job, another is already
#: queued behind it" case without polling hard forever.
FAST_WINDOW_SEC = 30.0


class _CrmLogHandler(logging.Handler):
    """Buffers this process's log lines so the agent loop can ship them to the CRM
    (`lead_gen_agent_logs`, T208). Bounded — a dead link drops the oldest, never blocks."""

    MAX = 500

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.rows: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:                                   # noqa: BLE001
            msg = record.getMessage()
        self.rows.append({
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": "warn" if record.levelno == logging.WARNING else record.levelname.lower(),
            "message": msg[:2000],
        })
        if len(self.rows) > self.MAX:
            del self.rows[: len(self.rows) - self.MAX]

    def drain(self) -> list[dict]:
        rows, self.rows = self.rows[:200], self.rows[200:]
        return rows


def _ship_agent_logs(cloud: "CrmCloud", handler: _CrmLogHandler) -> None:
    rows = handler.drain()
    if not rows:
        return
    try:
        cloud.agent_logs(rows)
    except (httpx.HTTPError, ValueError):
        # Put them back (front) so the next tick retries; the handler cap bounds it.
        handler.rows[:0] = rows


def _start_whoami_server() -> None:
    """T213 — `GET http://127.0.0.1:8766/whoami` → {device, root, git}. The CRM's "Detect
    this PC" button calls it from the browser: a web page cannot know which machine it is
    on, but it CAN reach loopback. CORS + Private-Network-Access headers so an https CRM
    origin may read it. Loopback only; nothing sensitive in the reply."""
    import json as _json
    import os
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from webscraper.config import ROOT
    port = int(os.getenv("LEAD_FINDER_LOCAL_PORT") or 8766)

    class H(BaseHTTPRequestHandler):
        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")

        def do_OPTIONS(self) -> None:                       # noqa: N802
            self.send_response(204); self._cors(); self.end_headers()

        def do_GET(self) -> None:                           # noqa: N802
            if self.path.split("?")[0] != "/whoami":
                self.send_response(404); self._cors(); self.end_headers(); return
            try:
                from .healthcheck import _git_rev
                git = _git_rev()
            except Exception:                               # noqa: BLE001
                git = None
            body = _json.dumps({"device": DEVICE_NAME, "root": str(ROOT), "git": git}).encode()
            self.send_response(200); self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

        def log_message(self, *a: Any) -> None:            # silence per-request noise
            pass

    def _serve() -> None:
        try:
            HTTPServer(("127.0.0.1", port), H).serve_forever()
        except OSError as e:
            log.info("whoami server not started on 127.0.0.1:%s (%s)", port, e)

    threading.Thread(target=_serve, name="whoami", daemon=True).start()


def run_agent(base: str, token: str, poll_sec: int = 5, kind: str = "saas") -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if kind == "crm":
        _start_whoami_server()
    crm_log = _CrmLogHandler()
    if kind == "crm":
        crm_log.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logging.getLogger().addHandler(crm_log)
        _CRM_LOG[:] = [crm_log]
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
    # Adaptive polling. A flat interval meant a job the user had just created sat at
    # "queued" for up to that whole interval before the agent even looked — with the
    # scheduled task's `--poll 15`, pressing Re-verify did nothing visible for 15s.
    # While there is work in flight (or just finished) we look every second; once
    # everything is quiet we fall back to `poll_sec`.
    ACTIVE_PHASES = ("queued", "waiting", "scraping", "enriching", "researching", "verifying_wa")
    last_busy = 0.0

    def _busy() -> bool:
        """Anything running locally, or anything still waiting to run."""
        if srv.worker.current_job is not None:
            return True
        row = store.conn.execute(
            f"SELECT 1 FROM jobs WHERE phase IN ({','.join('?' * len(ACTIVE_PHASES))}) LIMIT 1",
            ACTIVE_PHASES).fetchone()
        return row is not None

    while True:
        if kind == "crm":
            _poll_command(cloud)
            _ship_agent_logs(cloud, crm_log)
        try:
            _tick(cloud, store, kind, synced_upto)
        except httpx.HTTPError as e:
            log.warning("cloud unreachable: %s", e)
        try:
            if _busy():
                last_busy = time.monotonic()
        except Exception:                                  # noqa: BLE001
            # A busy-check failure must never stop the loop; fall back to the
            # configured interval, which is the old behaviour.
            log.debug("busy check failed", exc_info=True)
        fast = (time.monotonic() - last_busy) < FAST_WINDOW_SEC
        time.sleep(BUSY_POLL_SEC if fast else poll_sec)


#: The one command thread allowed at a time (a second QR window would only confuse).
_cmd_thread = None
#: The CRM log handler, so an `update` can flush its last lines before exiting.
_CRM_LOG: list = []


def _poll_command(cloud: "CrmCloud") -> None:
    """T202 - run a CRM-requested out-of-band command on this machine.

    `wa_login <label>` opens WhatsApp Web HEADED here (the browser window appears on this
    machine's screen - the agent runs on the user's PC/Mac, so that is where the QR must
    be scanned). Runs in its own thread so job polling continues; when it ends the
    self-check is re-sent on the next `jobs` call so the CRM health panel flips green
    without waiting the usual 5 minutes."""
    import threading
    global _cmd_thread
    if _cmd_thread is not None and _cmd_thread.is_alive():
        return
    _cmd_thread = None
    try:
        cmd = cloud.command()
    except (httpx.HTTPError, ValueError):
        return
    if not cmd:
        return

    def _run() -> None:
        global DEVICE_NAME
        ok, result = False, None
        try:
            if cmd["command"] == "wa_login":
                from webscraper import wa_verify
                label = (cmd.get("arg") or "main").strip() or "main"
                log.info("CRM asked for wa-login %r - opening WhatsApp Web, scan the QR", label)
                ok = wa_verify.login(label)
                result = f"linked {label}" if ok else "timed out waiting for the QR scan (2 min)"
            elif cmd["command"] == "rename":
                # The CRM already renamed its row + repointed jobs; from the next call on
                # this machine must heartbeat under the new label, and keep it after reboot.
                new = (cmd.get("arg") or "").strip()[:120]
                if new:
                    _remember_device_name(new)
                    DEVICE_NAME = new
                    ok, result = True, f"now reporting as {new}"
                else:
                    result = "empty name"
            elif cmd["command"] == "checks":
                # "Verify": re-run the self-check now instead of at the next 5-min mark.
                ok, result = True, "self-check re-sent"
            elif cmd["command"] == "update":
                # "Update agent" (T209): pull main, reinstall deps, then exit — the
                # supervisor loop (run-agent-loop.sh/.bat) restarts us on the new code.
                # A job in flight is left to _requeue_orphans on the way back up.
                import subprocess
                import sys
                from webscraper.config import ROOT
                before = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                        capture_output=True, text=True).stdout.strip()
                pull = subprocess.run(["git", "pull", "--ff-only"], cwd=ROOT,
                                      capture_output=True, text=True, timeout=120)
                if pull.returncode != 0:
                    result = f"git pull failed: {(pull.stderr or pull.stdout).strip()[:200]}"
                else:
                    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                                   cwd=ROOT, capture_output=True, text=True, timeout=600)
                    after = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                           capture_output=True, text=True).stdout.strip()
                    ok, result = True, (f"already on {after}" if after == before
                                        else f"updated {before} → {after}, restarting")
                    log.info("update: %s", result)
                    cloud.command_done(int(cmd["id"]), True, result)
                    _ship_agent_logs(cloud, _CRM_LOG[0]) if _CRM_LOG else None
                    import os as _os
                    _os._exit(0)
            else:
                result = f"unknown command {cmd['command']}"
        except Exception as e:                                    # noqa: BLE001
            log.warning("command %s failed: %s", cmd.get("command"), e)
            result = str(e)[:300]
        finally:
            cloud._checks_sent_at = 0.0          # re-send the self-check on the next tick
            try:
                cloud.command_done(int(cmd["id"]), ok, result)
            except httpx.HTTPError as e:
                log.warning("command_done failed: %s", e)

    _cmd_thread = threading.Thread(target=_run, name="agent-command", daemon=True)
    _cmd_thread.start()


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
    # it wastes the daily cap. 'unknown'/null are re-tried. W26: every candidate number of
    # a place is checked (Maps phone + WhatsApp link — the CRM `results` action does not
    # carry `site_phones`, so website numbers are only covered by the live job, not here),
    # and the unit of progress is NUMBERS.
    from webscraper.store import aggregate_wa, wa_candidates
    targets = [r for r in rows if wa_candidates(r) and r.get("wa_verified") not in ("yes", "no")]
    already = sum(1 for r in rows if r.get("wa_verified") in ("yes", "no"))
    total = sum(len(wa_candidates(r)) for r in targets)
    src_by_pk = {r["place_key"]: r.get("whatsapp_source") for r in targets}
    if already:
        log.info("re-verify #%s: skipping %d already-verified", jid, already)

    # A re-verify has exactly one phase, so it gets its ETA straight from the rolling
    # WhatsApp-check rate instead of the full job model. `None` (no history yet) is
    # passed through untouched — the CRM renders "estimating…" for it.
    started = time.monotonic()
    started_iso = datetime.now(timezone.utc).isoformat()

    def wa_progress(done: int, reason: str | None = None) -> dict:
        """`reason` set = the final picture: the lane ended with that token (completed /
        wa_daily_cap / ...). Without it the CRM saw a lane that never reported an ending
        and printed "Interrupted · the job stopped before this lane reported finishing"
        for a plain daily-cap stop (job #14, 2026-08-25)."""
        per = ((time.monotonic() - started) / done) if done >= 3 else store.phase_rate("verifying_wa")
        eta_sec = round(max(0, total - done) * per) if per else None
        src = "live" if done >= 3 else "history"
        ended = reason is not None
        if ended:
            eta_sec = None
        # A re-verify runs exactly one lane, so `lanes` is hand-built here rather than read
        # off a jobs row — there is no local job row for it (the work is driven straight off
        # the CRM's existing results). Same shape as eta.lanes() so the CRM renders it with
        # the same code path; the two idle lanes are reported 'disabled', not 'pending'.
        now_iso = datetime.now(timezone.utc).isoformat()
        wa_lane = {"key": "whatsapp", "label": eta.LANE_LABELS["whatsapp"],
                   "unit": eta.LANE_UNITS["whatsapp"],
                   "status": ("done" if reason == "completed" else "stopped") if ended else "running",
                   "done": done, "total": total, "total_is_min": False,
                   "eta_sec": eta_sec, "estimating": (eta_sec is None) and not ended, "rate_source": src,
                   "started_at": started_iso, "ended_at": now_iso if ended else None,
                   "ok": (reason == "completed") if ended else None, "reason": reason,
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
    # The CRM's Logs dialog reads the mirrored LOCAL job's job_logs; a re-verify has no
    # run of its own, so its lines go onto that job (T179).
    _lrow = store.conn.execute("SELECT id FROM jobs WHERE cloud_id=? ORDER BY id DESC LIMIT 1", (jid,)).fetchone()
    local_log_id = int(_lrow["id"]) if _lrow else None
    name_by_pk = {r["place_key"]: r for r in targets}

    def _jlog(msg: str, level: str = "info") -> None:
        if local_log_id is not None:
            try:
                store.log(local_log_id, "whatsapp", msg, level)
            except Exception:                                     # noqa: BLE001
                pass

    _jlog(f"re-verify: checking {total} number(s) on WhatsApp — accounts: "
          f"{', '.join(store.enabled_wa_accounts()) or 'none'}"
          + (" · no daily cap" if settings.wa_daily_cap <= 0 else f" · cap {settings.wa_daily_cap}/day"))

    # Per-number verdicts per place (W26); the place-level picture is re-derived after
    # every check with the same rule the live lane uses (any yes → yes, all no → no).
    checks: dict[str, list[dict[str, Any]]] = {}

    def _n_checked() -> int:
        return sum(len(v) for v in checks.values())

    def onp(pk: str, status: str, num: str | None = None, source: str | None = None) -> None:
        checks.setdefault(pk, []).append({"number": num, "source": source, "verdict": status})
        agg, wa_num = aggregate_wa(checks[pk])
        collected[pk] = agg
        from webscraper.lanes import _wa_line
        r0 = name_by_pk.get(pk, {})
        _jlog(_wa_line(r0, status, num, source))
        upd: dict[str, Any] = {"place_key": pk, "wa_verified": agg}
        # Confirmed hit -> promote the verified number + mark source 'verified' (drops an
        # 'unverified' guess). Every number a miss on a guessed number -> clear it.
        # 'assumed_mobile' is the retired spelling of 'unverified' (2026-08-23); still
        # accepted here so rows written before the migration still clear correctly.
        if agg == "yes" and wa_num:
            upd["whatsapp_number"] = wa_num
            upd["whatsapp_source"] = "verified"
        elif agg == "no" and src_by_pk.get(pk) in ("unverified", "assumed_mobile"):
            upd["whatsapp_number"] = None
            upd["whatsapp_source"] = None
        # Flush after EVERY check so the CRM's progress bar + ✓/✗ badges move live.
        try:
            cloud.progress(jid, "verifying_wa", wa_progress(_n_checked()))
            cloud.set_wa(jid, [upd])
        except httpx.HTTPError as e:
            log.warning("re-verify #%s: progress/set_wa failed: %s", jid, e)

    try:
        # job_id=None → verify_places won't touch the local store's places (they aren't
        # here); `store` is still used for WA account rotation + daily caps.
        res = wa_verify.verify_places(store, targets, on_progress=onp, job_id=None)
    except wa_verify.WaNotLoggedIn as e:
        cloud.done(jid, "error", f"WhatsApp verify skipped — {e}")
        return
    except Exception as e:  # noqa: BLE001
        log.exception("re-verify #%s failed", jid)
        cloud.done(jid, "error", str(e)[:200])
        return
    # Feed the rolling average so the next verify run can be estimated (W4).
    n_checked = _n_checked()
    store.record_phase_rate(None, "verifying_wa", n_checked, time.monotonic() - started)
    if collected:
        cloud.set_wa(jid, [{"place_key": k, "wa_verified": v} for k, v in collected.items()])
    capped = bool(res.get("capped")) if isinstance(res, dict) else False
    reason = "wa_daily_cap" if capped else "completed"
    left = max(0, total - n_checked)
    try:
        cloud.progress(jid, "verifying_wa", wa_progress(n_checked, reason))
    except httpx.HTTPError as e:
        log.warning("re-verify #%s: final progress failed: %s", jid, e)
    msg = (f"WhatsApp daily cap reached ({settings.wa_daily_cap}/account/day) — {n_checked} numbers checked, "
           f"{left} left; re-run tomorrow or add another WhatsApp account (wa-login)") if capped else None
    _jlog(f"re-verify finished: {n_checked} numbers checked across {len(collected)} lead(s), {left} left"
          + (" — daily cap hit" if capped else ""), "warn" if capped else "info")
    cloud.done(jid, "done", msg)
    log.info("re-verify #%s: %d numbers checked%s", jid, n_checked, " (daily cap hit)" if capped else "")


def _place_keys_json(cj: dict) -> str | None:
    """The CRM's `place_keys` array as JSON text, or None for "the whole job"."""
    keys = cj.get("place_keys")
    if isinstance(keys, list) and keys:
        import json as _j
        return _j.dumps([str(k) for k in keys])
    return None


def _requeue_rerun(cloud: "Cloud | CrmCloud", store: Store, cj: dict, kind: str) -> None:
    """Re-arm an ALREADY-MIRRORED cloud job that the CRM has queued again.

    Re-enrich / re-verify reuse the same cloud job id, so there is nothing new to
    create — the local row just has to be pointed at the new scope and put back in
    the queue. Without this the `mirrored` guard skipped it silently and the CRM
    showed "queued" for ever.
    """
    row = store.conn.execute(
        "SELECT id FROM jobs WHERE cloud_id=? AND cloud_kind=?", (cj["id"], kind)).fetchone()
    if not row:
        return
    if cloud.claim(cj["id"]) is None:
        return                                   # another agent got there first
    local_id = int(row["id"])
    store.update_job(
        local_id,
        phase="queued", status="running", note=None, finished_at=None,
        stop_requested=0,
        reenrich_only=int(bool(cj.get("reenrich_only", False))),
        discovery_pending=int(bool(cj.get("discovery_pending", False))),
        do_enrich=int(bool(cj.get("do_enrich", True))),
        do_wa_verify=int(bool(cj.get("do_wa_verify", False))),
        place_keys=_place_keys_json(cj),
        # Carry the re-run's window choice too. Without this the CRM's "Show window"
        # toggle was dropped on every re-run — the local job kept its original headless
        # value, so a re-enrich asked to run headed still ran hidden.
        headless=int(bool(cj.get("headless", True))),
        message="re-run requested from the CRM",
    )
    store.log(local_id, "job", f"re-run requested from the CRM (cloud job #{cj['id']})")
    srv.worker.wake.set()                        # do not wait out the poll interval
    log.info("cloud job #%s re-queued -> local job #%s", cj["id"], local_id)


#: local job id -> `changed_at` watermark of updates already streamed. In memory like
#: `synced_upto`; a restart re-sends one tick's worth of updates, absorbed by the upsert.
_changed_upto: dict[int, str] = {}


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
            # ALREADY MIRRORED — but the CRM may have queued it AGAIN. "Re-enrich" and
            # "Re-verify" re-queue the same cloud job rather than creating a new one, and
            # this guard used to drop them on the floor: the job sat at "queued" in the UI
            # for ever with an empty log, because nothing local ever started. (Observed on
            # live jobs #6 and #7, 2026-08-23.) `wa_verify_only` had its own bypass above;
            # every other re-run shape needs this one.
            if str(cj.get("status") or "") == "queued":
                _requeue_rerun(cloud, store, cj, kind)
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
                         # Carried from the very first mirror, not just on a re-run: a job
                         # created as a scoped re-enrich would otherwise start a full Maps
                         # scrape of everything.
                         reenrich_only=int(bool(cj.get("reenrich_only", False))),
                         discovery_pending=int(bool(cj.get("discovery_pending", False))),
                         place_keys=_place_keys_json(cj),
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
                # Rows the CRM already has, updated since the last tick (enrichment /
                # WhatsApp verdicts). Before the new-row stream so the watermark of "what
                # the CRM holds" is the one this query was scoped to.
                since = _changed_upto.get(row["id"], "")
                changed, ctop = store.places_changed_since(
                    row["id"], since, synced_upto.get(row["id"], 0))
                if changed:
                    try:
                        for i in range(0, len(changed), 200):
                            cloud.sync(cid, [_flat(c) for c in changed[i:i + 200]])
                        _changed_upto[row["id"]] = ctop
                        log.info("streamed %d updated lead(s) to job #%s", len(changed), cid)
                    except httpx.HTTPError as e:
                        log.warning("update stream to #%s failed (will retry next tick): %s", cid, e)
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
                # Push the FINAL lane picture first. The last progress tick predates the
                # lanes ending, so without this the CRM row kept every lane as
                # status=running / reason=None and had to guess: job #13 (2026-08-25)
                # showed enrichment + WhatsApp "Completed" on 0 / 0 after discovery died.
                try:
                    cloud.progress(cid, row["phase"], _local_progress(row, store))
                except httpx.HTTPError as e:
                    log.warning("final progress for #%s failed (done still sent): %s", cid, e)
                cloud.done(cid, "done" if row["phase"] == "done" else "error",
                           None if row["phase"] == "done" else failure[:300])
                store.update_job(row["id"], note="synced")
