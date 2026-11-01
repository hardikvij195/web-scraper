"""W213 (CRM T1493): `wa_reverify_unknown` must refresh live like the other WhatsApp knobs."""
from webscraper import agent


def test_reverify_unknown_is_live_refreshed():
    assert any("WA_REVERIFY_UNKNOWN".startswith(p) for p in agent._CONFIG_REFRESH_PREFIXES)
