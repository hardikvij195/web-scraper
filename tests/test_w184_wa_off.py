"""W184 (CRM T1104, 2026-10-11): "3 - ASUS" (8 GB) froze solid three times, each time right after the agent
launched the WhatsApp Chrome for its unlinked `main`, and on every restart resumed local job #411 (= CRM
#22708) the CRM had already re-queued / kept ASUS off.

  * `WA_OFF` / `WA_OFF__<DEVICE>` (CRM `wa_off__<device>`): no WhatsApp Chrome launch of any kind on this
    machine, `wa_free` false in capacity, a job's WhatsApp lane ends `wa_no_session` for the CRM failover.
  * `_requeue_orphans` asks the CRM (a claim) before resuming; a refusal drops the local row for good.

Fakes only: no Playwright, no Chrome, no network.
"""
from __future__ import annotations

import httpx
import pytest

from webscraper import agent, healthcheck, lanes as L, server as server_mod, wa_verify
from webscraper.store import Store
from tests.test_w111_resilience import _Cloud, _mid_run, st  # noqa: F401  (st = fixture)


@pytest.fixture()
def asus(monkeypatch):
    monkeypatch.setattr(agent, "DEVICE_NAME", "3 - ASUS")
    for k in ("WA_OFF", "WA_OFF__3 - ASUS", "WA_OFF__DELL"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _boom(*a, **kw):
    raise AssertionError("a WhatsApp Chrome was launched while wa_off is on")


# ── knob parsing ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("dev,gen,want", [
    (None, None, False), ("1", None, True), ("true", "0", True), ("0", "1", False),   # per-device wins
    (None, "yes", True), ("", "on", True), (" off ", None, False), ("ON", None, True),
])
def test_parse_wa_off(dev, gen, want):
    assert L.parse_wa_off(dev, gen) is want


def test_wa_off_reads_the_per_device_key(asus):
    assert L.wa_off() is False
    asus.setenv("WA_OFF__DELL", "1")                    # another machine's switch does nothing here
    assert L.wa_off() is False
    asus.setenv("WA_OFF__3 - ASUS", "1")
    assert L.wa_off() is True
    asus.setenv("WA_OFF", "1")
    asus.setenv("WA_OFF__3 - ASUS", "0")                # per-device "0" beats a generic "1"
    assert L.wa_off() is False


def test_wa_off_is_live_refreshed_from_the_crm():
    assert "WA_OFF__3 - ASUS".startswith(agent._CONFIG_REFRESH_PREFIXES)


# ── launch guards ─────────────────────────────────────────────────────────────────────
def test_no_whatsapp_chrome_launch_when_off(asus, tmp_path):
    asus.setenv("WA_OFF__3 - ASUS", "1")
    asus.setattr(wa_verify, "sync_playwright", _boom)
    asus.setattr(wa_verify._br, "launch_evicting", _boom)
    assert wa_verify.account_status("main") == "unknown"           # probe / W152 re-probe / relink proof
    with pytest.raises(wa_verify.WaOff):
        wa_verify.verify_places(None, [{"place_key": "k", "phone": "+64211234567"}])  # lane + re-verify
    with pytest.raises(wa_verify.WaOff):
        wa_verify._ensure_session(None, {}, None, "main")          # first open + Relauncher recovery
    asus.setattr(L, "_account_status", _boom)

    class _S:
        def flagged_wa_accounts(self):
            return ["main"]
    assert L._reprobe_flagged(object(), _S()) is False             # W152 re-probe never runs
    asus.setattr(wa_verify, "account_status", _boom)
    server_mod._refresh_wa_status(Store(tmp_path / "x.db"), "before job")   # boot / job-boundary probe


def test_wa_login_still_opens_but_warns(asus, caplog):
    asus.setenv("WA_OFF__3 - ASUS", "1")
    asus.setattr(wa_verify, "sync_playwright", lambda: (_ for _ in ()).throw(RuntimeError("opened")))
    with pytest.raises(Exception, match="opened"):
        wa_verify.login("main")
    assert "switched off" in caplog.text


def test_whatsapp_lane_hands_the_pass_back(asus, tmp_path):
    asus.setenv("WA_OFF__3 - ASUS", "1")
    s = Store(tmp_path / "lane.db")
    jid = s.create_job(query="q", location="Auckland", max_places=5, delay_sec=0, phase="queued")
    notes: list[str] = []
    polls = iter([False, True])

    class _Ctl:
        def other_lanes_done(self, lane):
            return next(polls)

    class _Lane:
        key, job_id, ctl = "whatsapp", jid, _Ctl()

        def note(self, m, lvl="info"):
            notes.append(m)

        def stopped(self):
            return False

    asus.setattr(L, "WA_RELINK_POLL_SEC", 0.0)
    asus.setattr(wa_verify, "verify_places", _boom)
    assert L.WhatsAppLane._wa_off_end(_Lane(), s) == L.R_WA_NO_SESSION
    assert notes == [L.WA_OFF_MSG]
    assert "wa_off" in L.WA_OFF_MSG and "another machine" in L.WA_OFF_MSG


# ── capacity / self-check ─────────────────────────────────────────────────────────────
def test_capacity_reports_wa_free_0_when_off(asus, tmp_path):
    db_path = tmp_path / "cap.db"
    asus.setattr(server_mod, "Store", lambda *a, **kw: Store(db_path))
    asus.setattr(server_mod, "max_inflight_jobs", lambda: 3)
    asus.setattr(server_mod, "memory_blocked", lambda: False)
    s = Store(db_path)
    s.add_wa_account("main")
    s.set_wa_status("main", "logged_in")
    s.close()
    w = server_mod.Worker()
    on = w.capacity()
    assert on["wa_free"] is True and on["wa_off"] is False
    asus.setenv("WA_OFF__3 - ASUS", "1")
    off = w.capacity()
    assert off["wa_free"] is False and off["wa_off"] is True
    assert off["lane_slots"]["whatsapp"]["account"] is False
    assert server_mod.wa_account_usable(Store(db_path)) is False


# ── stale local job: ownership check ──────────────────────────────────────────────────
@pytest.mark.parametrize("row,err,verdict,why", [
    ({"status": "claimed", "agent_device": "3 - ASUS"}, None, "resume", ""),
    ({"status": "claimed", "agent_device": "2 - DELL"}, None, "drop", "CRM gave it to 2 - DELL"),
    ({"status": "done", "agent_device": None}, None, "drop", "CRM job is done"),
    (None, "job is for 1 - PC", "drop", "CRM gave it to 1 - PC"),
    (None, "this machine lost this job 2+ times in 6 h — kept off it, another machine takes it (T1101)",
     "drop", "CRM kept this machine off it (T1101)"),
    (None, "job is incomplete", "drop", "CRM job is incomplete"),
    (None, "claimed by another agent", "ask", "claimed by another agent"),
    (None, "WhatsApp is the next lane and this machine WhatsApp lane is busy / has no session — queued next",
     "drop", None),
    (None, None, "resume", ""),
])
def test_resume_verdict(row, err, verdict, why):
    v, w = agent.resume_verdict("3 - ASUS", row=row, error=err)
    assert v == verdict
    if why is not None:
        assert w == why


class _ClaimCloud(_Cloud):
    def __init__(self, verdicts: dict, **kw):
        super().__init__(cancelled=set(), **kw)
        self.verdicts = verdicts

    def claim_verdict(self, jid):
        v = self.verdicts[jid]
        if isinstance(v, Exception):
            raise v
        return v


def test_requeue_drops_a_job_the_crm_kept_off_this_machine(st, monkeypatch, caplog):  # noqa: F811
    monkeypatch.setattr(agent, "DEVICE_NAME", "3 - ASUS")
    mine, pinned, kept, offline = (_mid_run(st, 22700), _mid_run(st, 22708),
                                   _mid_run(st, 22709), _mid_run(st, 22710))
    cloud = _ClaimCloud({22700: ({"status": "claimed", "agent_device": "3 - ASUS"}, None),
                         22708: (None, "job is for 2 - DELL"),
                         22709: (None, "this machine lost this job 2+ times in 6 h — kept off it"),
                         22710: httpx.ConnectError("offline")})
    assert agent._requeue_orphans(st, "crm", cloud) == 2              # mine + offline (old behaviour)
    assert st.get_job(mine)["phase"] == "queued" and st.get_job(offline)["phase"] == "queued"
    for jid in (pinned, kept):
        g = st.get_job(jid)
        assert g["phase"] == "stopped" and g["note"] == "synced" and g["stop_requested"] == 1
    assert f"dropped local job #{pinned} (CRM #22708): CRM gave it to 2 - DELL (W184)" in caplog.text
    assert cloud.pings == [22700, 22710]                              # dropped jobs are never pinged
    assert agent._requeue_orphans(st, "crm", cloud) == 0              # dropped rows never come back
    assert st.get_job(pinned)["phase"] == "stopped" and st.get_job(kept)["phase"] == "stopped"
