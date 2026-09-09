"""W94 (CRM T533): WhatsApp Web's own sync splash is 'syncing', never 'blank' / logged out."""
from webscraper import wa_verify


class _Loc:
    def __init__(self, n=0, text=""):
        self._n, self._t = n, text

    def count(self):
        return self._n

    def inner_text(self, timeout=None):
        return self._t


class _Page:
    """A fake page: `states` is the sequence _boot_state should see, one per poll."""

    def __init__(self, states):
        self.states = list(states)
        self._last = self.states[0]

    def _cur(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def locator(self, sel):
        st = self._cur() if sel == wa_verify._CHAT_SEL else self._last
        if sel == wa_verify._CHAT_SEL:
            self._last = st
        if sel == wa_verify._CHAT_SEL:
            return _Loc(1 if st == "chat" else 0)
        if sel == wa_verify._QR_SEL:
            return _Loc(1 if st == "qr" else 0)
        text = {"syncing": "WhatsApp\nEnd-to-end encrypted\nDon't close this window. Your messages are downloading.",
                "blank": ""}.get(st, "")
        return _Loc(0, text)


def test_boot_state_classification():
    assert wa_verify._boot_state(_Page(["chat"])) == "chat"
    assert wa_verify._boot_state(_Page(["qr"])) == "qr"
    assert wa_verify._boot_state(_Page(["syncing"])) == "syncing"
    assert wa_verify._boot_state(_Page(["blank"])) == "blank"


def test_wait_boot_outlives_blank_window_while_syncing(monkeypatch):
    monkeypatch.setattr(wa_verify.time, "sleep", lambda s: None)
    # sync splash for a few polls, then the chat list — must return 'chat', not 'blank'
    assert wa_verify.wait_boot(_Page(["syncing", "syncing", "syncing", "chat"]), "t", blank_ms=1) == "chat"
    # sync that never finishes: capped by sync_max_sec, reported as 'syncing'
    assert wa_verify.wait_boot(_Page(["syncing"]), "t", blank_ms=1, sync_max_sec=0) == "syncing"
    # nothing at all: the short blank window applies
    assert wa_verify.wait_boot(_Page(["blank"]), "t", blank_ms=1) == "blank"
    # a QR wins immediately
    assert wa_verify.wait_boot(_Page(["qr"]), "t", blank_ms=1) == "qr"


def test_sync_max_env(monkeypatch):
    monkeypatch.delenv("WA_SYNC_MAX_SEC", raising=False)
    assert wa_verify.wa_sync_max_sec() == wa_verify.WA_SYNC_MAX_SEC
    monkeypatch.setenv("WA_SYNC_MAX_SEC", "5")
    assert wa_verify.wa_sync_max_sec() == 30.0          # floor
    monkeypatch.setenv("WA_SYNC_MAX_SEC", "600")
    assert wa_verify.wa_sync_max_sec() == 600.0
