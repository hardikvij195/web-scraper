"""W154 (CRM T1047): a WhatsApp profile last SEEN logged out is not a usable account for the claim
side (`server.wa_account_usable`) — DELL claimed WhatsApp work for 20 min on a QR screen. Unknown /
never-probed still counts; a later `logged_in` sighting (W152) brings it back."""
from __future__ import annotations

from pathlib import Path

from webscraper import server as S
from webscraper.store import Store


def test_logged_out_profile_is_not_usable(tmp_path: Path):
    s = Store(tmp_path / "t.db")
    s.add_wa_account("main")
    assert S.wa_account_usable(s) is True                    # never probed: unknown is not "lost"
    s.set_wa_status("main", "logged_out")
    assert S.wa_account_usable(s) is False
    s.set_wa_status("main", "logged_in")
    assert S.wa_account_usable(s) is True
    s.add_wa_account("spare")
    s.set_wa_status("spare", "logged_out")
    assert S.wa_account_usable(s) is True, "one linked account is enough"
    s.close()
