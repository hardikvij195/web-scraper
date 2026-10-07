"""W171 (owner directive 2026-10-07 16:50, "make sure false flags do not come again").

MI 2026-10-07 15:24: the W143 ladder flagged `needs_relink` at episode 4 purely because WhatsApp Web
never finished syncing four slices in a row while Maps held 84-87 % RAM. The CRM to-do told the owner
to press Start session; the probe at 15:46 answered "already linked". Now:

  * episode 4 PROBES the profile (`wa_verify._relink_probe` -> `account_status`): only a rendered
    link-device / QR screen ('logged_out') flags `needs_relink`; 'logged_in' / 'unknown' benches the
    account `sync_stuck` (info log, no owner to-do) and the ladder resets.
  * `sync_stuck` is out of `enabled_wa_accounts` / `pick_wa_account` (the lane runs on as if nothing
    were linked, the CRM moves the WA pass) and in `flagged_wa_accounts` (the W152 re-probe covers it);
    any `logged_in` sighting (`Store.set_wa_status`) clears both flags.
  * the self-check label says STUCK SYNCING and contains neither "linked" nor "NEEDS RELINK", so the
    CRM's `lead_gen_wa_usable` reads the machine as not WA-usable without raising a to-do.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from webscraper import lanes as L, wa_verify as wv
from webscraper.store import Store
from tests.test_w115_wa_sync_requeue import _Page, _rows, wa  # noqa: F401  (fixture)
from tests.test_w143_resync_failsafe import _S, _always_syncing, _clean_ladder  # noqa: F401  (autouse fixture)
from tests.test_w144_lane_caps import _relink_lane


def _at_episode_4(monkeypatch, proof: str) -> tuple[_S, list[str]]:
    _always_syncing(monkeypatch)
    probed: list[str] = []
    monkeypatch.setattr(wv, "_relink_probe", lambda name: probed.append(name) or proof)
    monkeypatch.setattr(wv._br, "kill_profile_holder", lambda d, reason="": True)
    wv._RESYNC_EPISODES["acc1"] = 3                                # the next zero-verdict slice is episode 4

    class _Ctx:
        def close(self):
            pass

    def ensure(pw, open_ctx, rl, name, headless=None):
        return open_ctx.setdefault(name, (_Ctx(), _Page()))[1]
    monkeypatch.setattr(wv, "_ensure_session", ensure)
    return _S(), probed


# ── (a) / (b): a probe that does NOT see the QR screen benches, never flags ──────────
@pytest.mark.parametrize("proof", ["logged_in", "unknown"])
def test_episode_4_without_a_qr_screen_benches_sync_stuck(wa, monkeypatch, proof):
    st, probed = _at_episode_4(monkeypatch, proof)
    res = wv.verify_places(st, _rows(25), job_id=7)
    assert probed == ["acc1"], "episode 4 must probe before deciding"
    assert res["sync_stuck"] == 1 and res["needs_relink"] == 0 and res["sync_blocked"] is False
    assert st.stuck == {"acc1": True} and st.flags == {}, "needs_relink must not be set without proof"
    assert any(m.startswith("WhatsApp [acc1] is linked but WhatsApp Web on this machine is stuck syncing (4 episodes, ")
               and m.endswith("— benched here, retrying every 5 min; no QR needed") for m in st.logs)
    assert not any("needs a fresh QR relink" in m for m in st.logs)
    assert "acc1" not in wv._RESYNC_EPISODES, "the ladder resets after the bench"
    with pytest.raises(wv.WaNotLoggedIn, match="stuck syncing on this machine .*acc1"):
        wv.verify_places(st, _rows(1), job_id=7)              # benched = no account to pick


def test_bench_log_is_info_not_error(wa, monkeypatch):
    st, _ = _at_episode_4(monkeypatch, "logged_in")
    levels: list[tuple[str, str]] = []
    st.log = lambda job_id, lane, message, level="info": levels.append((message, level))  # type: ignore[method-assign]
    wv.verify_places(st, _rows(25), job_id=7)
    bench = [(msg, lvl) for msg, lvl in levels if "stuck syncing" in msg]
    assert bench and all(lvl == "info" for _, lvl in bench), bench
    assert not any(lvl == "error" and ("relink" in msg or "QR" in msg) for msg, lvl in levels), "a bench must never log a relink / QR error"


# ── (c): the QR screen is the only proof ─────────────────────────────────────────────
def test_episode_4_with_a_qr_screen_flags_needs_relink(wa, monkeypatch):
    st, probed = _at_episode_4(monkeypatch, "logged_out")
    res = wv.verify_places(st, _rows(25), job_id=7)
    assert probed == ["acc1"]
    assert res["needs_relink"] == 1 and res["sync_stuck"] == 0
    assert st.flags == {"acc1": True} and st.stuck == {}
    assert any("needs a fresh QR relink — the phone removed this device" in m for m in st.logs)


def test_a_probe_that_throws_answers_unknown(monkeypatch):
    def boom(name):
        raise RuntimeError("chrome would not launch")
    monkeypatch.setattr(wv, "account_status", boom)
    assert wv._relink_probe("acc1") == "unknown"


# ── (d) / (e): store semantics ───────────────────────────────────────────────────────
def test_sync_stuck_is_flagged_but_not_enabled_and_logged_in_clears_it(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.add_wa_account("main")
    s.set_wa_status("main", "logged_in")
    assert s.enabled_wa_accounts() == ["main"] and s.flagged_wa_accounts() == []
    s.set_wa_sync_stuck("main", True)
    row = s.list_wa_accounts()[0]
    assert row["sync_stuck"] == 1 and row["sync_stuck_at"] and row["needs_relink"] == 0
    assert s.enabled_wa_accounts() == [], "benched = out of rotation"
    assert s.pick_wa_account(0, "2026-10-07") is None
    assert s.flagged_wa_accounts() == ["main"], "benched = re-probed by the relink wait"
    assert s.wa_relinked_since("2000-01-01") is False
    s.set_wa_status("main", "logged_out")                 # a QR screen does not un-bench by itself
    assert s.flagged_wa_accounts() == ["main"]
    s.set_wa_status("main", "logged_in")                  # (e) the chat list clears the bench
    assert s.list_wa_accounts()[0]["sync_stuck"] == 0 and s.list_wa_accounts()[0]["sync_stuck_at"] is None
    assert s.enabled_wa_accounts() == ["main"] and s.flagged_wa_accounts() == []


def test_wa_account_usable_excludes_a_benched_account(tmp_path: Path):
    from webscraper.server import wa_account_usable
    s = Store(tmp_path / "t.db")
    s.add_wa_account("main")
    s.set_wa_status("main", "logged_in")
    assert wa_account_usable(s) is True
    s.set_wa_sync_stuck("main", True)
    assert wa_account_usable(s) is False, "the WA pass must move to another machine while benched"


# ── (e) the relink-wait re-probe un-benches on a chat list ───────────────────────────
def test_reprobe_clears_a_benched_account_with_the_synced_note(monkeypatch):
    lane, store = _relink_lane()
    store.flagged_wa_accounts = lambda: ["main"]
    store.list_wa_accounts = lambda: [{"name": "main", "disabled": 0, "sync_stuck": 1, "needs_relink": 0}]
    monkeypatch.setattr(L, "memory_high_for_whatsapp", lambda: False)
    monkeypatch.setattr(L, "_account_status", lambda name: "logged_in")
    assert L._reprobe_flagged(lane, store) is True
    assert any("WhatsApp [main] synced again — benched flag cleared, WhatsApp lane resuming" in n for n in lane.notes)
    assert not any("false alarm" in n for n in lane.notes)


def test_reprobe_turns_a_bench_into_a_true_flag_only_on_the_qr_screen(monkeypatch):
    lane, store = _relink_lane()
    writes: list[tuple[str, str, bool]] = []
    store.flagged_wa_accounts = lambda: ["main"]
    store.list_wa_accounts = lambda: [{"name": "main", "disabled": 0, "sync_stuck": 1, "needs_relink": 0}]
    store.set_wa_sync_stuck = lambda name, flag: writes.append(("stuck", name, flag))
    store.set_wa_needs_relink = lambda name, flag: writes.append(("relink", name, flag))
    monkeypatch.setattr(L, "memory_high_for_whatsapp", lambda: False)
    monkeypatch.setattr(L, "_account_status", lambda name: "unknown")
    assert L._reprobe_flagged(lane, store) is False
    assert writes == [] and any("still stuck syncing" in n and "no QR needed" in n for n in lane.notes)
    monkeypatch.setattr(L, "_account_status", lambda name: "logged_out")
    assert L._reprobe_flagged(lane, store) is False
    assert writes == [("stuck", "main", False), ("relink", "main", True)]


# ── (f) the self-check label the CRM parses ──────────────────────────────────────────
def test_healthcheck_label_for_a_benched_account(monkeypatch, tmp_path: Path):
    from webscraper import healthcheck as hc
    path = tmp_path / "t.db"
    s = Store(path)
    s.add_wa_account("main")
    s.set_wa_status("main", "logged_in")
    s.set_wa_sync_stuck("main", True)
    (tmp_path / "profiles" / "main" / "Default").mkdir(parents=True)
    monkeypatch.setattr(hc.settings, "wa_profiles_dir", tmp_path / "profiles")
    monkeypatch.setattr("webscraper.store.Store", lambda *a, **k: Store(path))
    chk = hc._wa_session()
    assert chk["ok"] is False, "a benched account is not a linked one for the ok flag"
    assert chk["detail"].startswith("WhatsApp accounts — main: STUCK SYNCING (WhatsApp Web on this machine never finished syncing since ")
    assert chk["detail"].endswith("— session intact, auto-retry every 5 min, no action needed)")
    assert "linked" not in chk["detail"] and "NEEDS RELINK" not in chk["detail"]
    s.set_wa_status("main", "logged_in")                  # synced again
    chk = hc._wa_session()
    assert chk["ok"] is True and "main: linked" in chk["detail"]
