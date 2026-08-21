from webscraper.extractors import (
    classify_social, clean_email, clean_url, contact_page_links, decode_cf_email, domain_of,
    extract_emails, extract_socials, extract_whatsapp, is_probably_mobile, normalise_phone, normalise_wa,
)


def test_country_from_address():
    from webscraper.extractors import country_from_address
    assert country_from_address("c/o STONE, 52 Cornhill, London EC3V 3PD, United Kingdom") == "GB"
    assert country_from_address("759/53, Ferguson College Rd, Deccan Gymkhana, Pune, Maharashtra 411004") is None
    assert country_from_address("12 Main St, Springfield, IL 62701, USA") == "US"
    assert country_from_address("Sheikh Zayed Rd, Dubai, United Arab Emirates") == "AE"
    assert country_from_address("1 Raffles Pl, Singapore 048616") is None   # no trailing country name
    assert country_from_address(None) is None


def test_region_of_phone():
    from webscraper.extractors import region_of_phone
    assert region_of_phone("+442076365774", "IN") == "GB"
    assert region_of_phone("+919876543210", "GB") == "IN"
    assert region_of_phone(None, "IN") == "IN"
    # the real job-7 case: UK lead, job country IN, wa.me without country code
    assert normalise_wa("7858260539", region_of_phone("+442076365774", "IN")) == "447858260539"


def test_normalise_wa():
    assert normalise_wa("7858260539", "GB") == "447858260539"      # UK mobile w/o country code
    assert normalise_wa("919876543210", "GB") == "919876543210"    # already E.164 digits
    assert normalise_wa("+91 98765-43210", "IN") == "919876543210"
    assert normalise_wa("9876543210", "IN") == "919876543210"
    assert normalise_wa(None) is None


def test_clean_url():
    assert clean_url("https://a.com/x/?utm_source=google&utm_medium=organic&utm_campaign=GMB") == "https://a.com/x/"
    assert clean_url("https://a.com/?page=2&fbclid=abc") == "https://a.com/?page=2"
    assert clean_url("https://a.com/") == "https://a.com/"
    assert clean_url(None) is None


def test_cf_email():
    key = 0x5A
    enc = bytes([key] + [ord(c) ^ key for c in "test@x.com"]).hex()
    assert decode_cf_email(enc) == "test@x.com"
    assert extract_emails(f'<a href="/cdn-cgi/l/email-protection#{enc}">mail</a>') == ["test@x.com"]
    assert extract_emails(f'<span data-cfemail="{enc}"></span>') == ["test@x.com"]

HTML = """
<html><body>
<a href="mailto:Hello@Example-Clinic.com?subject=hi">mail</a>
<p>Reach us at info [at] smile-dental [dot] in or sales@smile-dental.in</p>
<img src="logo@2x.png">
<script>var e='noreply@smile-dental.in'; x='abc@sentry.io'</script>
<a href="https://www.instagram.com/smiledental/">ig</a>
<a href="https://www.instagram.com/p/abc123/">post</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>
<a href="https://www.facebook.com/SmileDentalPune/?ref=x">fb</a>
<a href="https://in.linkedin.com/company/smile-dental">li</a>
<a href="https://twitter.com/intent/tweet?text=x">tw-intent</a>
<a href="https://x.com/smiledental">x</a>
<a href="https://www.youtube.com/watch?v=abc">yt-video</a>
<a href="https://www.youtube.com/@smiledental">yt</a>
<a href="https://api.whatsapp.com/send?phone=919876543210&text=Hi">wa</a>
<a href="/contact-us">Contact</a>
<a href="/about">About us</a>
<a href="https://other.com/contact">ext</a>
</body></html>
"""


def test_emails():
    got = extract_emails(HTML)
    assert got[0] == "hello@example-clinic.com"          # mailto first
    assert "info@smile-dental.in" in got                  # de-obfuscated
    assert "sales@smile-dental.in" in got
    assert "noreply@smile-dental.in" not in got
    assert "abc@sentry.io" not in got
    assert all(not e.endswith(".png") for e in got)


def test_clean_email_filters():
    assert clean_email("Foo.Bar@Example.ORG") is None       # example.org blocked
    assert clean_email("x@y.com.") == "x@y.com"
    assert clean_email("logo@2x.png") is None
    assert clean_email("user@domain.com") is None


def test_socials():
    s = extract_socials(HTML)
    assert s["instagram"] == "https://instagram.com/smiledental"
    assert s["facebook"] == "https://facebook.com/SmileDentalPune"
    assert s["linkedin"] == "https://in.linkedin.com/company/smile-dental"
    assert s["twitter_x"] == "https://x.com/smiledental"
    assert s["youtube"] == "https://youtube.com/@smiledental"


def test_classify_social_rejects_widgets():
    assert classify_social("https://www.facebook.com/sharer/sharer.php?u=x") is None
    assert classify_social("https://twitter.com/share") is None
    assert classify_social("https://www.instagram.com/") is None
    assert classify_social("https://www.linkedin.com/feed/") is None
    assert classify_social("https://www.facebook.com/profile.php?id=123") == ("facebook", "https://facebook.com/profile.php?id=123")


def test_whatsapp():
    assert extract_whatsapp(HTML) == "919876543210"
    assert extract_whatsapp('<a href="https://wa.me/+91-98765-43210">chat</a>') == "919876543210"
    assert extract_whatsapp('<a href="whatsapp://send?phone=919999999999">x</a>') == "919999999999"
    assert extract_whatsapp("<p>no wa here</p>") is None


def test_phone():
    assert normalise_phone("098765 43210", "IN") == ("+919876543210", "919876543210")
    assert normalise_phone("+1 415 555 2671", "IN")[0] == "+14155552671"
    assert normalise_phone(None) == (None, None)
    assert is_probably_mobile("098765 43210", "IN") is True
    assert is_probably_mobile("020 2612 3456", "IN") is False


def test_domain_and_contact_links():
    assert domain_of("https://www.Smile-Dental.in/path?x=1") == "smile-dental.in"
    links = contact_page_links(HTML, "https://smile-dental.in/")
    assert links == ["https://smile-dental.in/contact-us", "https://smile-dental.in/about"]
