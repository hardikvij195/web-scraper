"""W211 (CRM T1491): email extraction must be linear-time — a long [A-Za-z0-9] run with no '@' made
`EMAIL_RE.findall` quadratic and held the GIL for minutes (PC/ASUS/MI went silent mid-crawl)."""
import time

from webscraper.extractors import EMAIL_RE, _find_emails, extract_emails

SAMPLES = [
    "contact: info@example.co.uk or sales@shop.example.com.",
    "a@b@c.com",
    "x" * 100 + "@toolong.com and ok@fine.org",
    "mail me at john.doe+tag@sub.domain.io!",
    "no email here at all",
    "trailing @ sign @",
    "two@one.com,three@two.net;four@three.org",
    "weird@-dash.com dots..@x.com",
]


def test_same_matches_as_the_old_regex_for_real_addresses():
    for s in SAMPLES:
        old = [m for m in EMAIL_RE.findall(s) if len(m.rpartition("@")[0]) <= 64]
        assert _find_emails(s) == old, s


def test_linear_on_a_long_run_without_at():
    blob = "data:image/png;base64," + "A" * 400_000 + " info@acmeplumbing.ca " + "b" * 300_000
    html = f"<html><body><p>{blob}</p><img src='{blob}'></body></html>"
    t = time.perf_counter()
    found = extract_emails(html)
    assert time.perf_counter() - t < 3.0
    assert "info@acmeplumbing.ca" in found


def test_linear_on_long_whitespace_runs():
    html = "<html><body>" + (" " * 200_000) + "write to me [at] example [dot] com" + "</body></html>"
    t = time.perf_counter()
    extract_emails(html)
    assert time.perf_counter() - t < 3.0


def test_deobfuscated_text_still_found():
    html = "<html><body>Email: jane [at] acmeplumbing [dot] ca</body></html>"
    assert "jane@acmeplumbing.ca" in extract_emails(html)
