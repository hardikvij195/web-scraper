"""W207 (CRM T1477): N in-app WhatsApp lookups in flight per account (`WA_INAPP_CONCURRENCY`, 1..4);
the per-account delay runs once per N in-app verdicts. Fakes only: no Playwright, no Chrome."""
from __future__ import annotations

from webscraper import wa_verify as wv
from tests.test_w115_wa_sync_requeue import _rows, wa  # noqa: F401  (fixture)
from tests.test_w205_wa_inapp import _Page, _clean, _run  # noqa: F401  (autouse fixture)


class _APage(_Page):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.aheads: list[list] = []

    def evaluate(self, js, arg=None):
        if arg is not None:
            self.aheads.append(list(arg.get("ahead") or []))
        return super().evaluate(js, arg)


def test_setting_default_and_cap(monkeypatch):
    monkeypatch.delenv("WA_INAPP_CONCURRENCY", raising=False)
    assert wv.wa_inapp_concurrency() == 1
    monkeypatch.setenv("WA_INAPP_CONCURRENCY", "3")
    assert wv.wa_inapp_concurrency() == 3
    monkeypatch.setenv("WA_INAPP_CONCURRENCY", "9")
    assert wv.wa_inapp_concurrency() == 4
    monkeypatch.setenv("WA_INAPP_CONCURRENCY", "0")
    assert wv.wa_inapp_concurrency() == 1


def test_default_sends_no_ahead(wa, monkeypatch):
    monkeypatch.delenv("WA_INAPP_CONCURRENCY", raising=False)
    page = _APage(inapp=lambda n: "yes")
    _run(monkeypatch, page, 5, [])
    assert all(a == [] for a in page.aheads)


def test_three_in_flight_and_delay_once_per_batch(wa, monkeypatch):
    monkeypatch.setenv("WA_INAPP_CONCURRENCY", "3")
    sleeps: list[float] = []
    monkeypatch.setattr(wv.time, "sleep", lambda s: sleeps.append(s))
    page = _APage(inapp=lambda n: "yes")
    rec: list[str] = []
    res = _run(monkeypatch, page, 12, rec)
    assert res["checked"] == 12 and rec == ["yes"] * 12          # every number has its own verdict row
    assert page.aheads[0] == ["919876540001", "919876540002"]     # current + the next 2 in flight
    assert page.aheads[-1] == []
    # 3 calibration checks (send URL) each wait; the 9 in-app verdicts wait once per 3
    assert len(sleeps) == wv.WA_INAPP_CALIBRATE + 9 // 3


def test_feed_mode_takes_n_minus_1_ahead_and_hands_back(wa, monkeypatch):
    monkeypatch.setenv("WA_INAPP_CONCURRENCY", "4")
    page = _APage(inapp=lambda n: "yes")
    feed = wv.WaFeed(_rows(6))
    res = _run(monkeypatch, page, 0, [], feed=feed)
    assert res["checked"] == 6 and len(feed) == 0
    assert len(page.aheads[0]) == 3


def test_stop_hands_the_look_ahead_back(wa, monkeypatch):
    monkeypatch.setenv("WA_INAPP_CONCURRENCY", "4")
    page = _APage(inapp=lambda n: "yes")
    feed = wv.WaFeed(_rows(8))
    n = [0]

    def stop():
        n[0] += 1
        return n[0] > 2                       # two numbers checked, then stop

    monkeypatch.setattr(wv, "_ensure_session",
                        lambda pw, open_ctx, rl, name, headless=None: open_ctx.setdefault(name, (type("C", (), {"close": lambda s: None})(), page))[1])
    monkeypatch.setattr(wv, "_boot_state", lambda pg: "chat")
    monkeypatch.setattr(wv, "_decide", lambda pg, timeout_ms=None: "yes")
    monkeypatch.setattr(wv, "wa_delay_range", lambda: (0.0, 0.0))
    from tests.test_w115_wa_sync_requeue import _Store
    class _S(_Store):
        def record_wa_check(self, *a):
            pass

    res = wv.verify_places(_S(), feed, job_id=7, account="acc1", should_stop=stop)
    assert res["checked"] == 2
    assert len(feed) == 6                     # every unchecked number (incl. the look-ahead) is back
