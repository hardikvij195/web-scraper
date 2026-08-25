"""Pure functions: pull emails / social links / WhatsApp numbers out of HTML, classify phones.

No I/O here — everything is unit-testable with plain strings.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlsplit, urlunsplit

import phonenumbers
from selectolax.parser import HTMLParser

# ── emails ────────────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", re.I)

# Obvious non-contact addresses that show up in markup/JS bundles.
_EMAIL_BAD_DOMAINS = (
    "example.com", "example.org", "sentry.io", "wixpress.com",
    "w3.org", "schema.org", "googleapis.com", "google.com", "gstatic.com", "jquery.com",
    "yourdomain.com", "domain.com", "email.com", "test.com", "mysite.com", "company.com",
    "mailchimp.com", "localhost",
)
_EMAIL_BAD_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".js", ".css", ".woff", ".woff2", ".ico")
_EMAIL_BAD_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster", "abuse")


def clean_email(e: str) -> str | None:
    e = e.strip().strip(".,;:()[]<>\"'").lower()
    if not EMAIL_RE.fullmatch(e):
        return None
    local, _, domain = e.rpartition("@")
    if domain in _EMAIL_BAD_DOMAINS or any(domain.endswith("." + d) for d in _EMAIL_BAD_DOMAINS):
        return None
    if e.endswith(_EMAIL_BAD_EXT):
        return None
    if any(local.startswith(b) for b in _EMAIL_BAD_LOCAL):
        return None
    if len(local) > 64 or len(e) > 254:
        return None
    # filenames like image@2x.png slip through the regex when the ext is unknown
    if re.fullmatch(r"[a-z0-9_\-]+@\d+x\.[a-z]+", e):
        return None
    return e


_CF_EMAIL_RE = re.compile(r'(?:data-cfemail="|/cdn-cgi/l/email-protection#)([0-9a-fA-F]{2,})', re.I)


def decode_cf_email(hexstr: str) -> str | None:
    """Cloudflare 'email protection' obfuscation: first byte is the XOR key."""
    try:
        b = bytes.fromhex(hexstr)
    except ValueError:
        return None
    if len(b) < 2:
        return None
    key = b[0]
    try:
        return bytes(x ^ key for x in b[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def extract_emails(html: str) -> list[str]:
    """mailto: links first (most reliable), then Cloudflare-protected, then regex over text + HTML."""
    found: list[str] = []
    seen: set[str] = set()

    def add(e: str | None) -> None:
        if e and e not in seen:
            seen.add(e)
            found.append(e)

    tree = HTMLParser(html)
    for m in _CF_EMAIL_RE.finditer(html):
        add(clean_email(decode_cf_email(m.group(1)) or ""))
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.lower().startswith("mailto:"):
            target = unquote(href[7:]).split("?", 1)[0]
            for part in target.split(","):
                add(clean_email(part))
    # de-obfuscated text patterns: "name [at] domain [dot] com"
    text = tree.body.text(separator=" ") if tree.body else tree.text()
    deobf = re.sub(r"\s*[\[\(]\s*at\s*[\]\)]\s*", "@", text, flags=re.I)
    deobf = re.sub(r"\s*[\[\(]\s*dot\s*[\]\)]\s*", ".", deobf, flags=re.I)
    for m in EMAIL_RE.findall(deobf):
        add(clean_email(m))
    for m in EMAIL_RE.findall(html):
        add(clean_email(m))
    return found


# ── socials ───────────────────────────────────────────────────────────────────
_SOCIAL_HOSTS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com", "fb.me"),
    "linkedin": ("linkedin.com",),
    "twitter_x": ("twitter.com", "x.com"),
    "youtube": ("youtube.com", "youtu.be"),
    "tiktok": ("tiktok.com",),
}

# Paths that are share/intent/login widgets rather than a profile.
_SOCIAL_BAD_PATH = {
    "instagram": ("/p/", "/reel/", "/reels/", "/explore", "/accounts/", "/stories/", "/share", "/tv/"),
    "facebook": ("/sharer", "/share", "/dialog", "/login", "/tr", "/plugins", "/groups/", "/events/",
                 "/hashtag/", "/photo", "/watch", "/story.php", "/permalink", "/posts/", "/help"),
    "linkedin": ("/share", "/shareArticle", "/sharing", "/feed", "/login", "/pulse/", "/posts/", "/jobs/"),
    "twitter_x": ("/intent/", "/share", "/home", "/search", "/i/", "/hashtag/", "/login", "/status/"),
    "youtube": ("/watch", "/embed/", "/playlist", "/results", "/shorts/", "/feed"),
    "tiktok": ("/tag/", "/music/", "/video/", "/discover", "/embed"),
}

_TWITTER_RESERVED = {"share", "intent", "home", "search", "i", "hashtag", "login", "explore", "settings", "privacy", "tos"}


def _host(netloc: str) -> str:
    host = (netloc or "").lower()
    for pre in ("www.", "m.", "mobile."):
        if host.startswith(pre):
            host = host[len(pre):]
    return host


def _norm_social(url: str) -> str:
    """Drop query/fragment/utm and trailing slash; force https + bare host."""
    s = urlsplit(url.strip())
    path = re.sub(r"/+$", "", s.path or "")
    return urlunsplit(("https", _host(s.netloc), path, "", ""))


def classify_social(url: str) -> tuple[str, str] | None:
    """Return (network, normalised_profile_url) if `url` looks like a profile/page link."""
    if not url or url.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    try:
        s = urlsplit(url)
    except ValueError:
        return None
    host = _host(s.netloc)
    path = s.path or "/"
    for net, hosts in _SOCIAL_HOSTS.items():
        if host in hosts or any(host.endswith("." + h) for h in hosts):
            if any(bad in path for bad in _SOCIAL_BAD_PATH[net]):
                return None
            segs = [p for p in path.split("/") if p]
            if net in ("instagram", "twitter_x", "tiktok", "facebook") and not segs:
                return None  # bare instagram.com etc. = generic footer link
            if net == "twitter_x" and segs and segs[0].lower() in _TWITTER_RESERVED:
                return None
            if net == "linkedin" and (not segs or segs[0] not in ("company", "in", "school", "showcase")):
                return None
            if net == "youtube" and host != "youtu.be" and (
                not segs or not (segs[0].startswith("@") or segs[0] in ("channel", "c", "user"))
            ):
                return None
            if net == "facebook" and segs and segs[0] == "profile.php":
                qs = parse_qs(s.query)
                pid = (qs.get("id") or [None])[0]
                return (net, f"https://facebook.com/profile.php?id={pid}") if pid else None
            return net, _norm_social(url)
    return None


def extract_socials(html: str) -> dict[str, str]:
    """First good profile link per network, scanning <a href> in document order."""
    out: dict[str, str] = {}
    tree = HTMLParser(html)
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        hit = classify_social(href)
        if hit and hit[0] not in out:
            out[hit[0]] = hit[1]
        if len(out) == len(_SOCIAL_HOSTS):
            break
    return out


# ── whatsapp ──────────────────────────────────────────────────────────────────
_WA_RE = re.compile(
    r"(?:https?://)?(?:"
    r"(?:api\.|web\.)?whatsapp\.com/send/?\?[^\"'\s>]*?phone=(?P<p1>\+?[\d\-\s%]+)"
    r"|wa\.me/(?P<p2>\+?\d[\d\-]{6,})"
    r"|whatsapp://send\?[^\"'\s>]*?phone=(?P<p3>\+?[\d\-]+)"
    r")",
    re.I,
)


def _digits(s: str) -> str:
    return re.sub(r"\D", "", unquote(s or ""))


def extract_whatsapp(html: str) -> str | None:
    """Digits of the first wa.me / api.whatsapp.com / whatsapp:// number on the page."""
    for m in _WA_RE.finditer(html):
        num = _digits(m.group("p1") or m.group("p2") or m.group("p3") or "")
        if 8 <= len(num) <= 15:
            return num
    return None


# ── phones ────────────────────────────────────────────────────────────────────
_TEL_HREF_RE = re.compile(r"""href\s*=\s*["']tel:([^"']+)["']""", re.I)
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


def extract_phones(html: str, region: str = "IN", limit: int = 6) -> list[str]:
    """Every phone number a page lists, E.164 with '+', in order of appearance (W26).

    `tel:` links first — a site that bothers to link a number means it — then the visible
    text through phonenumbers' matcher (VALID leniency, so a random 10-digit id does not
    pass). WhatsApp deep links are NOT included; `extract_whatsapp` owns those. Capped so a
    directory page cannot hand the WhatsApp lane hundreds of numbers for one business."""
    if not html:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        try:
            n = phonenumbers.parse(unquote(raw).strip(), region)
        except phonenumbers.NumberParseException:
            return
        if not phonenumbers.is_valid_number(n):
            return
        e = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
        if e not in seen:
            seen.add(e)
            out.append(e)

    for m in _TEL_HREF_RE.finditer(html):
        add(m.group(1))
        if len(out) >= limit:
            return out
    text = _TAG_RE.sub(" ", html[:300_000])
    try:
        for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
            add(m.raw_string)
            if len(out) >= limit:
                break
    except Exception:                                             # noqa: BLE001 — matcher is best-effort
        pass
    return out


def normalise_phone(raw: str | None, country: str = "IN") -> tuple[str | None, str | None]:
    """Return (E.164 string, digits-only) or (None, None)."""
    if not raw:
        return None, None
    try:
        n = phonenumbers.parse(raw, country)
        if not phonenumbers.is_possible_number(n):
            return None, None
        e164 = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
        return e164, e164.lstrip("+")
    except phonenumbers.NumberParseException:
        d = _digits(raw)
        return (None, d) if d else (None, None)


def is_probably_mobile(raw: str | None, country: str = "IN") -> bool:
    """Heuristic for 'this number could be on WhatsApp' when the site shows no wa.me link."""
    if not raw:
        return False
    try:
        n = phonenumbers.parse(raw, country)
    except phonenumbers.NumberParseException:
        return False
    t = phonenumbers.number_type(n)
    return t in (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)


# Country name (as Google Maps prints it at the end of an address) → ISO region.
# Maps omits the country for places inside the browser's own region (en-IN → Indian
# addresses end at the PIN code), so the job's country remains the fallback.
_COUNTRY_NAMES: dict[str, str] = {
    "india": "IN", "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "northern ireland": "GB", "united states": "US", "usa": "US", "u.s.a.": "US", "u.s.": "US",
    "united arab emirates": "AE", "uae": "AE", "australia": "AU", "canada": "CA", "singapore": "SG",
    "new zealand": "NZ", "ireland": "IE", "germany": "DE", "france": "FR", "spain": "ES", "italy": "IT",
    "netherlands": "NL", "belgium": "BE", "switzerland": "CH", "austria": "AT", "portugal": "PT",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI", "poland": "PL", "czechia": "CZ",
    "czech republic": "CZ", "greece": "GR", "turkey": "TR", "türkiye": "TR", "israel": "IL",
    "saudi arabia": "SA", "qatar": "QA", "kuwait": "KW", "oman": "OM", "bahrain": "BH", "egypt": "EG",
    "south africa": "ZA", "kenya": "KE", "nigeria": "NG", "pakistan": "PK", "bangladesh": "BD",
    "sri lanka": "LK", "nepal": "NP", "malaysia": "MY", "indonesia": "ID", "thailand": "TH",
    "vietnam": "VN", "philippines": "PH", "japan": "JP", "south korea": "KR", "china": "CN",
    "hong kong": "HK", "taiwan": "TW", "mexico": "MX", "brazil": "BR", "argentina": "AR", "chile": "CL",
    "colombia": "CO", "peru": "PE", "russia": "RU", "ukraine": "UA", "romania": "RO", "hungary": "HU",
    "mauritius": "MU", "maldives": "MV", "bhutan": "BT",
}


def country_from_address(address: str | None) -> str | None:
    """ISO region from the trailing country name of a Maps address, else None."""
    if not address:
        return None
    parts = [p.strip().lower() for p in address.split(",") if p.strip()]
    # last segment, then second-to-last (in case the last is a postcode-only segment)
    for seg in reversed(parts[-2:]):
        seg = re.sub(r"\s+", " ", seg)
        if seg in _COUNTRY_NAMES:
            return _COUNTRY_NAMES[seg]
        # "London EC3V 3PD, United Kingdom" is clean, but guard against "411004 india"
        words = seg.split(" ")
        for n in (3, 2, 1):
            tail = " ".join(words[-n:])
            if tail in _COUNTRY_NAMES:
                return _COUNTRY_NAMES[tail]
    return None


def region_of_phone(e164: str | None, default: str = "IN") -> str:
    """ISO region of an E.164 phone ('+44…' → 'GB'); `default` when unknown.
    Lets a UK lead in a job configured for IN still get +44 on its WhatsApp number."""
    if not e164:
        return default
    try:
        n = phonenumbers.parse(e164, None)
    except phonenumbers.NumberParseException:
        return default
    return phonenumbers.region_code_for_number(n) or default


def normalise_wa(digits: str | None, country: str = "IN") -> str | None:
    """WhatsApp link numbers are often written without a country code (wa.me/7858260539).
    Return E.164 digits (no '+') using `country` as the fallback region."""
    if not digits:
        return None
    d = _digits(digits)
    if not d:
        return None
    for candidate, region in ((f"+{d}", None), (d, country)):
        try:
            n = phonenumbers.parse(candidate, region)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(n):
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164).lstrip("+")
    return d


_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "gad_source", "msclkid", "mc_cid", "mc_eid", "ref", "srsltid")


def clean_url(url: str | None) -> str | None:
    """Drop tracking query params Google/Meta bolt onto business websites."""
    if not url:
        return None
    try:
        s = urlsplit(url)
    except ValueError:
        return url
    if not s.query:
        return url
    kept = [kv for kv in s.query.split("&")
            if kv and not any(kv.lower().startswith(p) for p in _TRACKING_PARAMS)]
    return urlunsplit((s.scheme, s.netloc, s.path, "&".join(kept), ""))


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    except ValueError:
        return None
    return host[4:] if host.startswith("www.") else (host or None)


def contact_page_links(html: str, base_url: str) -> list[str]:
    """Same-site links that look like contact/about pages, in order found (max 4)."""
    tree = HTMLParser(html)
    base_dom = domain_of(base_url)
    out: list[str] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        if domain_of(full) != base_dom:
            continue
        path = urlparse(full).path.lower()
        text = (a.text() or "").strip().lower()
        if re.search(r"contact|reach|get-in-touch|about|connect|enquir|inquir", path + " " + text):
            full = full.split("#", 1)[0]
            if full not in out and full.rstrip("/") != base_url.rstrip("/"):
                out.append(full)
        if len(out) >= 4:
            break
    return out
