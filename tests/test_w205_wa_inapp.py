"""W205 (CRM T1472, MAC 2026-10-30): WhatsApp checks run INSIDE the booted WhatsApp Web page (the app's own
contact-exists query) instead of a full send-URL reload per number. The first verdicts per account are
cross-checked against the send URL; a disagreement or repeated errors switch the account back. Also: the
boot sync-out keeps the page open (W204 keep-alive that never ran).

Fakes only: no Playwright, no Chrome, no `data/leads.db`.
"""
from __future__ import annotations

import pytest

from webscraper import wa_verify as wv
from tests.test_w115_wa_sync_requeue import _Store, _rows, wa  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("WA_INAPP", "1")
    for d in (wv._INAPP_OFF, wv._INAPP_AGREE, wv._INAPP_N, wv._INAPP_ERRS):
        d.clear()
    yield
    for d in (wv._INAPP_OFF, wv._INAPP_AGREE, wv._INAPP_N, wv._INAPP_ERRS):
        d.clear()


class _Page:
    """A booted WhatsApp Web page: `evaluate` answers the in-app query, `goto` is the old send-URL path."""

    def __init__(self, inapp=lambda num: "yes", reload_on=None):
        self.inapp, self.reload_on = inapp, reload_on
        self.gotos: list[str] = []
        self.evals = 0
        self.nexts: list = []
        self.mark = "m1"

    def evaluate(self, js, arg=None):
        if arg is None:
            return self.mark
        num = arg["num"]
        self.nexts.append(arg.get("next"))
        self.evals += 1
        if self.reload_on is not None and num == self.reload_on:
            self.mark = "m2"                           # the document reloaded under the lookup
        v = self.inapp(num)
        if isinstance(v, Exception):
            raise v
        return {"v": v, "mark": self.mark} if v in ("yes", "no") else {"err": v, "mark": self.mark}

    def goto(self, url, **kw):
        self.gotos.append(url)


def test_setting_env_and_mac_default(monkeypatch):
    assert wv.wa_inapp_enabled() is True
    monkeypatch.setenv("WA_INAPP", "0")
    assert wv.wa_inapp_enabled() is False
    monkeypatch.delenv("WA_INAPP")
    import webscraper.agent as ag
    monkeypatch.setattr(ag, "DEVICE_NAME", "2 - MAC")
    assert wv.wa_inapp_enabled() is True
    monkeypatch.setattr(ag, "DEVICE_NAME", "4 - DELL")
    assert wv.wa_inapp_enabled() is False


def test_lookup_reads_yes_no_and_never_navigates(monkeypatch):
    monkeypatch.setattr(wv, "_boot_state", lambda page: "chat")
    p = _Page(inapp=lambda n: "no" if n.endswith("0") else "yes")
    assert wv.inapp_lookup(p, "acc1", "919876540000") == "no"
    assert wv.inapp_lookup(p, "acc1", "919876540001") == "yes"
    assert p.gotos == []


def test_lookup_off_the_chat_list_or_after_a_reload_falls_back(monkeypatch):
    monkeypatch.setattr(wv, "_boot_state", lambda page: "syncing")
    assert wv.inapp_lookup(_Page(), "acc1", "1") is None          # not booted -> send URL
    monkeypatch.setattr(wv, "_boot_state", lambda page: "chat")
    assert wv.inapp_lookup(_Page(reload_on="2"), "acc1", "2") is None   # marker lost = reloaded


def test_repeated_errors_switch_the_account_off(monkeypatch):
    monkeypatch.setattr(wv, "_boot_state", lambda page: "chat")
    p = _Page(inapp=lambda n: "no queryWidExists")
    for _ in range(wv.WA_INAPP_MAX_ERRS):
        assert wv.inapp_lookup(p, "acc1", "1") is None
    assert "acc1" in wv._INAPP_OFF
    assert wv.inapp_lookup(_Page(), "acc1", "1") is None          # stays off for the process


def test_audit_mismatch_switches_off_and_agreement_counts():
    assert wv.inapp_needs_audit("acc1")
    assert wv.inapp_note_audit("acc1", "yes", "yes")
    assert wv.inapp_note_audit("acc1", "no", "unknown")           # nothing to compare: no verdict either way
    assert wv._INAPP_AGREE["acc1"] == 1
    assert not wv.inapp_note_audit("acc1", "no", "yes")
    assert "acc1" in wv._INAPP_OFF


def _run(monkeypatch, page, n, rec, feed=None):
    class _Ctx:
        def close(self):
            pass

    class _S(_Store):
        def record_wa_check(self, job_id, pk, e164, source, status, name):
            rec.append(status)

    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None: open_ctx.setdefault(name, (_Ctx(), page))[1])
    monkeypatch.setattr(wv, "_boot_state", lambda pg: "chat")
    monkeypatch.setattr(wv, "_decide", lambda pg, timeout_ms=None: "yes")
    monkeypatch.setattr(wv, "wa_delay_range", lambda: (0.0, 0.0))
    return wv.verify_places(_S(), feed if feed is not None else _rows(n), job_id=7, account="acc1")


def test_only_the_calibration_numbers_reload(wa, monkeypatch):
    rec: list[str] = []
    page = _Page(inapp=lambda n: "yes")
    res = _run(monkeypatch, page, 10, rec)
    assert res["checked"] == 10 and res["yes"] == 10
    assert len(page.gotos) == wv.WA_INAPP_CALIBRATE               # 3 cross-checks, 7 numbers with no reload
    assert page.evals == 10
    assert "acc1" not in wv._INAPP_OFF


def test_a_disagreement_falls_back_to_the_send_url_for_the_rest(wa, monkeypatch):
    rec: list[str] = []
    page = _Page(inapp=lambda n: "no")                            # in-app says no, the send URL says yes
    res = _run(monkeypatch, page, 6, rec)
    assert res["yes"] == 6                                        # the send-URL verdict is what is recorded
    assert "acc1" in wv._INAPP_OFF
    assert len(page.gotos) == 6


def test_audit_cadence_setting(monkeypatch):
    wv._INAPP_AGREE["acc1"] = wv.WA_INAPP_CALIBRATE
    monkeypatch.setenv("WA_INAPP_AUDIT_EVERY", "200")
    wv._INAPP_N["acc1"] = 50
    assert not wv.inapp_needs_audit("acc1")
    wv._INAPP_N["acc1"] = 200
    assert wv.inapp_needs_audit("acc1")
    monkeypatch.setenv("WA_INAPP_AUDIT_EVERY", "0")
    assert not wv.inapp_needs_audit("acc1")


def test_prefetch_passes_the_next_number_only_when_on(wa, monkeypatch):
    rec: list[str] = []
    page = _Page(inapp=lambda n: "yes")
    _run(monkeypatch, page, 5, rec)
    assert set(page.nexts) == {None}                          # off by default
    for d in (wv._INAPP_OFF, wv._INAPP_AGREE, wv._INAPP_N, wv._INAPP_ERRS):
        d.clear()
    monkeypatch.setenv("WA_INAPP_PREFETCH", "1")
    page2 = _Page(inapp=lambda n: "yes")
    res = _run(monkeypatch, page2, 5, rec)
    assert res["checked"] == 5
    assert page2.nexts[0] == "919876540001" and page2.nexts[-1] is None   # next queued number, none at the end


def test_prefetch_look_ahead_in_feed_mode(wa, monkeypatch):
    monkeypatch.setenv("WA_INAPP_PREFETCH", "1")
    rec: list[str] = []
    page = _Page(inapp=lambda n: "yes")
    res = _run(monkeypatch, page, 0, rec, feed=wv.WaFeed(_rows(4)))
    assert res["checked"] == 4
    assert page.nexts[:3] == ["919876540001", "919876540002", "919876540003"]
