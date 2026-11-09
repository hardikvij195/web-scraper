"""W215 (CRM T1557): WhatsApp per-NUMBER check budget + cooldown."""
import pytest

from webscraper import wa_budget
from webscraper.wa_budget import Budget, account_number


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WA_NUMBER_CAP", "30")
    monkeypatch.setenv("WA_NUMBER_COOLDOWN_MIN", "120")
    monkeypatch.delenv("WA_NUMBER_CAP_NUMBERS", raising=False)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class FakeCrm:
    """Mirrors wa_number_take: shared per-number counter across machines."""
    def __init__(self, clock, cap=30, cool=7200):
        self.clock, self.cap, self.cool = clock, cap, cool
        self.rows: dict[str, dict] = {}
        self.calls = 0
        self.down = False

    def __call__(self, number, n):
        self.calls += 1
        if self.down:
            raise ConnectionError("crm down")
        r = self.rows.setdefault(number, {"checks": 0, "until": None})
        if r["until"] and r["until"] > self.clock():
            return {"allowed": 0, "cooldown_until": r["until"]}
        if r["until"]:
            r.update(checks=0, until=None)
        allow = min(n, max(0, self.cap - r["checks"]))
        r["checks"] += allow
        if r["checks"] >= self.cap:
            r["until"] = self.clock() + self.cool
        return {"allowed": allow, "cooldown_until": r["until"]}


def test_number_parsing():
    assert account_number("poco_biz_9315646352") == "9315646352"
    assert account_number("iphone_9821243899") == "9821243899"
    assert account_number("main") is None
    assert account_number(None) is None
    assert wa_budget.applies("9315646352")
    assert wa_budget.applies("9773603818")                  # default `all` (OnePlus logged out too)
    assert not wa_budget.applies(None)


def test_numbers_list_all_and_off(monkeypatch):
    monkeypatch.setenv("WA_NUMBER_CAP_NUMBERS", "all")
    assert wa_budget.applies("9773603818")
    monkeypatch.setenv("WA_NUMBER_CAP_NUMBERS", "+91 97736 03818")
    assert wa_budget.applies("9773603818") and not wa_budget.applies("9315646352")
    monkeypatch.setenv("WA_NUMBER_CAP", "0")
    assert not wa_budget.applies("9773603818")


def test_cap_reached_then_resumes_after_cooldown(tmp_path):
    clock = Clock()
    crm = FakeCrm(clock)
    b = Budget(remote=crm, now=clock, state_path=tmp_path / "b.json")
    name = "poco_biz_9315646352"
    results = [b.take(name) for _ in range(30)]
    assert all(ok for ok, _, _ in results)
    assert crm.calls == 3                                    # reserved in blocks of 10
    ok, until, ev = b.take(name)
    assert not ok and ev == "start" and until == pytest.approx(clock.t + 7200)
    assert b.cooling_names([name, "main"]) == {name}
    ok, _, ev = b.take(name)
    assert not ok and ev is None                             # announced once
    assert b.take("main")[0]                                 # no number -> never capped
    clock.t += 7201
    assert b.cooling_until(name) is None
    ok, _, ev = b.take(name)
    assert ok and ev == "end"


def test_same_number_shared_across_machines(tmp_path):
    clock = Clock()
    crm = FakeCrm(clock)
    mac = Budget(remote=crm, now=clock)
    dell = Budget(remote=crm, now=clock)
    n = 0
    while mac.take("realme_biz_9211256289")[0] and dell.take("realme_biz_9211256289")[0]:
        n += 2
    assert n <= 30
    assert not dell.take("realme_biz_9211256289")[0] or not mac.take("realme_biz_9211256289")[0]


def test_crm_down_falls_back_to_local_cap(tmp_path):
    clock = Clock()
    crm = FakeCrm(clock)
    crm.down = True
    b = Budget(remote=crm, now=clock, state_path=tmp_path / "b.json")
    oks = [b.take("mi_biz_8287659741")[0] for _ in range(31)]
    assert oks[:30] == [True] * 30 and oks[30] is False
    # the cooldown survives a restart (state file)
    b2 = Budget(remote=crm, now=clock, state_path=tmp_path / "b.json")
    assert b2.cooling_until("mi_biz_8287659741")
    clock.t += 7201
    assert b2.take("mi_biz_8287659741")[0]


def test_no_remote_is_local_only():
    clock = Clock()
    b = Budget(remote=None, now=clock)
    assert all(b.take("iphone_9821243899")[0] for _ in range(30))
    assert not b.take("iphone_9821243899")[0]
