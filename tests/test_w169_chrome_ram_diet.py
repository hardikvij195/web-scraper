"""W169 (CRM T1047): the Chrome RAM diet — lean launch args, Maps asset blocking by resource
type, low-RAM recycle cadence, WhatsApp window mode under memory pressure."""
import re

import pytest

from webscraper import chrome_args, maps, wa_verify
from webscraper.browser_recovery import RESTORE_BUBBLE_ARGS

EXPECTED = {
    "--disable-features=site-per-process,IsolateOrigins,TranslateUI,BackForwardCache",
    "--renderer-process-limit=2", "--disable-dev-shm-usage", "--no-first-run",
    "--no-default-browser-check", "--disable-sync", "--disable-component-update",
    "--disable-extensions", "--metrics-recording-only", "--disable-background-networking",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("CHROME_LEAN_ARGS", "CHROME_JS_HEAP_MB", "CHROME_JS_HEAP_MB__WA", "MAPS_BLOCK_ASSETS",
              "MAPS_RELAUNCH_LOWMEM_DIVISOR", "MAPS_RELAUNCH_EVERY_PLACES", "MAPS_RELAUNCH_EVERY_TILES",
              "WA_LOWMEM_HIDDEN_PCT"):
        monkeypatch.delenv(k, raising=False)


# ---- lean_args -------------------------------------------------------------------------

def test_lean_args_exact_list_per_kind():
    assert set(chrome_args.lean_args("maps")) == EXPECTED | {"--js-flags=--max-old-space-size=256"}
    assert set(chrome_args.lean_args("wa")) == EXPECTED | {"--js-flags=--max-old-space-size=512"}
    # the enrichment Chrome keeps its stealth: no flags IGNORE_DEFAULT_ARGS deliberately strips
    enrich = chrome_args.lean_args("enrich")
    assert "--disable-component-update" not in enrich and "--disable-extensions" not in enrich
    assert "--renderer-process-limit=2" in enrich


def test_lean_args_no_duplicates_and_no_forbidden():
    for kind in ("maps", "wa", "enrich"):
        args = chrome_args.lean_args(kind)
        names = [a.split("=", 1)[0] for a in args]
        assert len(names) == len(set(names)), kind
        assert not any(a in chrome_args.FORBIDDEN for a in args)
        assert all(a.startswith("--") for a in args)


def test_lean_args_env_off(monkeypatch):
    monkeypatch.setenv("CHROME_LEAN_ARGS", "0")
    assert chrome_args.lean_args("maps") == []
    assert maps._launch_kwargs.__name__  # module imports fine with the diet off
    monkeypatch.setenv("CHROME_LEAN_ARGS", "off")
    assert chrome_args.lean_args("wa") == []


def test_js_heap_env(monkeypatch):
    monkeypatch.setenv("CHROME_JS_HEAP_MB", "0")
    assert not any(a.startswith("--js-flags") for a in chrome_args.lean_args("maps"))
    monkeypatch.setenv("CHROME_JS_HEAP_MB__WA", "768")
    assert "--js-flags=--max-old-space-size=768" in chrome_args.lean_args("wa")
    assert not any(a.startswith("--js-flags") for a in chrome_args.lean_args("maps"))


def test_merge_args_dedupes_and_unions_features():
    merged = chrome_args.merge_args(
        ["--lang=en-IN", *RESTORE_BUBBLE_ARGS, "--disable-features=Foo"], chrome_args.lean_args("maps"))
    names = [a.split("=", 1)[0] for a in merged]
    assert len(names) == len(set(names))
    assert merged.count("--no-first-run") == 1
    feat = [a for a in merged if a.startswith("--disable-features=")]
    assert feat == ["--disable-features=Foo,site-per-process,IsolateOrigins,TranslateUI,BackForwardCache"]
    assert merged[0] == "--lang=en-IN"                        # the explicit per-launch flag stays first
    # an explicit value beats the diet's
    m2 = chrome_args.merge_args(["--renderer-process-limit=4"], chrome_args.lean_args("maps"))
    assert "--renderer-process-limit=4" in m2 and "--renderer-process-limit=2" not in m2


def test_every_launch_site_carries_the_diet():
    mk = maps._launch_kwargs(maps.opener_profile_dir(), headless=False)
    assert mk["headless"] is False                              # Maps stays headed (lite panel otherwise)
    assert "--renderer-process-limit=2" in mk["args"] and mk["args"].count("--no-first-run") == 1
    for mode in ("visible", "hidden", "headless"):
        wk = wa_verify._launch_kwargs(mode)
        assert "--js-flags=--max-old-space-size=512" in wk["args"], mode
        assert len(wk["args"]) == len(set(wk["args"])), mode
    from webscraper import browser_fetch
    merged = chrome_args.merge_args(browser_fetch.LAUNCH_ARGS, chrome_args.lean_args("enrich"))
    assert "--disable-extensions" not in merged and merged.count("--disable-dev-shm-usage") == 1


# ---- Maps asset route ------------------------------------------------------------------

@pytest.mark.parametrize("rtype", ["image", "media", "font", "Image"])
def test_route_blocks_assets(rtype):
    assert maps.should_block_asset(rtype, "https://maps.gstatic.com/x")


@pytest.mark.parametrize("rtype", ["document", "script", "xhr", "fetch", "stylesheet", "other", "websocket", None])
def test_route_keeps_the_rest(rtype):
    assert not maps.should_block_asset(rtype, "https://www.google.com/maps/place/x?hl=en")


def test_route_url_regex_fallback():
    assert maps.should_block_asset("other", "https://lh3.googleusercontent.com/p/a.jpg?w=100")
    assert not maps.should_block_asset("other", "https://www.google.com/maps/vt/pb=!1m4")


def test_route_env_off(monkeypatch):
    assert maps.block_assets_enabled()
    monkeypatch.setenv("MAPS_BLOCK_ASSETS", "0")
    assert not maps.block_assets_enabled()


class _Route:
    def __init__(self, rtype, url):
        class R: pass
        self.request = R(); self.request.resource_type = rtype; self.request.url = url
        self.calls = []
    def abort(self): self.calls.append("abort")
    def continue_(self): self.calls.append("continue")


def test_asset_route_handler():
    r = _Route("image", "https://maps.gstatic.com/tile"); maps._asset_route(r); assert r.calls == ["abort"]
    r = _Route("stylesheet", "https://www.gstatic.com/maps.css"); maps._asset_route(r); assert r.calls == ["continue"]
    r = _Route("xhr", "https://www.google.com/maps/preview/place"); maps._asset_route(r); assert r.calls == ["continue"]


# ---- low-RAM recycle cadence -----------------------------------------------------------

def test_lowmem_cadence_math():
    assert maps.lowmem_cadence(40, 8000.0, False) == 20
    assert maps.lowmem_cadence(20, 8500.0, False) == 10            # boundary is inclusive
    assert maps.lowmem_cadence(40, 16000.0, False) == 40
    assert maps.lowmem_cadence(40, 8000.0, True) == 40             # explicit override untouched
    assert maps.lowmem_cadence(0, 8000.0, False) == 0              # off stays off
    assert maps.lowmem_cadence(40, None, False) == 40              # unknown RAM -> default
    assert maps.lowmem_cadence(40, 8000.0, False, divisor=4) == 10
    assert maps.lowmem_cadence(1, 8000.0, False) == 1              # never below 1


def test_relaunch_every_uses_lowmem(monkeypatch):
    monkeypatch.setattr(maps, "_TOTAL_MB_CACHE", [8000.0])
    monkeypatch.setattr(maps, "_device_upper", lambda: "TESTBOX")
    monkeypatch.delenv("MAPS_RELAUNCH__TESTBOX", raising=False)
    assert maps.opener_relaunch_every() == 20 and maps.collect_relaunch_every() == 10
    monkeypatch.setenv("MAPS_RELAUNCH_LOWMEM_DIVISOR", "4")
    assert maps.opener_relaunch_every() == 10
    monkeypatch.setenv("MAPS_RELAUNCH_EVERY_PLACES", "30")        # generic env = explicit -> untouched
    assert maps.opener_relaunch_every() == 30
    monkeypatch.setattr(maps, "_TOTAL_MB_CACHE", [32000.0])
    monkeypatch.delenv("MAPS_RELAUNCH_EVERY_PLACES")
    assert maps.opener_relaunch_every() == 40 and maps.collect_relaunch_every() == 20


# ---- WhatsApp window mode under pressure -----------------------------------------------

def test_pick_window_mode():
    assert wa_verify.pick_window_mode("visible", 85.0, 85.0) == "hidden"
    assert wa_verify.pick_window_mode("visible", 84.9, 85.0) == "visible"
    assert wa_verify.pick_window_mode("visible", None, 85.0) == "visible"
    assert wa_verify.pick_window_mode("visible", 99.0, 0) == "visible"       # threshold 0 = never
    assert wa_verify.pick_window_mode("headless", 99.0, 85.0) == "headless"  # never promoted to headless
    assert wa_verify.pick_window_mode("hidden", 99.0, 85.0) == "hidden"


def test_window_mode_under_pressure(monkeypatch):
    from webscraper import healthcheck
    monkeypatch.setattr(healthcheck, "_memory", lambda: {"used_pct": 91.0})
    assert wa_verify.window_mode_under_pressure("visible", "acc") == "hidden"
    monkeypatch.setattr(healthcheck, "_memory", lambda: {"used_pct": 40.0})
    assert wa_verify.window_mode_under_pressure("visible", "acc") == "visible"
    monkeypatch.setenv("WA_LOWMEM_HIDDEN_PCT", "30")
    assert wa_verify.window_mode_under_pressure("visible", "acc") == "hidden"
    assert wa_verify.window_mode_under_pressure("headless", "acc") == "headless"
    monkeypatch.setattr(healthcheck, "_memory", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert wa_verify.window_mode_under_pressure("visible", "acc") == "visible"
