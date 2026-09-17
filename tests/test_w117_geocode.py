"""W117 (CRM T759): geocode the job location; drifted Maps results are marked far instead
of inheriting the job's country. No network — pure parsing / distance functions only."""
from webscraper.geocode import GeoHit, parse_nominatim_response
from webscraper.maps import (
    center_drift_km, haversine_km, is_place_far_from_geocode, place_far_km,
)

# A trimmed but shape-accurate Nominatim jsonv2 response for "Sandwick, United Kingdom".
SANDWICK_JSON = [{
    "lat": "60.0012",
    "lon": "-1.2431",
    "display_name": "Sandwick, Shetland Islands, Scotland, United Kingdom",
    "address": {"village": "Sandwick", "country": "United Kingdom", "country_code": "gb"},
}]


def test_parse_nominatim_response_happy_path():
    hit = parse_nominatim_response(SANDWICK_JSON)
    assert hit is not None
    assert hit.lat == 60.0012
    assert hit.lng == -1.2431
    assert hit.country_code == "gb"
    assert "Sandwick" in hit.display_name


def test_parse_nominatim_response_empty_or_bad():
    assert parse_nominatim_response([]) is None
    assert parse_nominatim_response(None) is None
    assert parse_nominatim_response("not a list") is None
    assert parse_nominatim_response([{"lat": "not a number", "lon": "1"}]) is None
    assert parse_nominatim_response([{"lat": "1", "lon": "1"}]) is not None   # no address block -> OK, no cc


def test_sandwick_vs_noida_drift_is_far():
    # Production case: job #1291's places clustered near Noida/Ghaziabad, India (~28.6N 77.3E)
    # while the job's real location, Sandwick, is Shetland (~60.0N -1.24E).
    sandwick = GeoHit(lat=60.0012, lng=-1.2431, country_code="gb", display_name="Sandwick")
    noida_pin = (28.61, 77.34)
    is_far, km = is_place_far_from_geocode(noida_pin[0], noida_pin[1], sandwick, radius_km=None)
    assert is_far
    assert km > place_far_km(None)


def test_london_croydon_is_kept():
    london = GeoHit(lat=51.5072, lng=-0.1276, country_code="gb", display_name="London")
    croydon = (51.3762, -0.0982)          # ~15 km from central London
    is_far, km = is_place_far_from_geocode(croydon[0], croydon[1], london, radius_km=25.0)
    assert not is_far
    assert km < place_far_km(25.0)


def test_no_geocode_never_rejects():
    is_far, km = is_place_far_from_geocode(28.61, 77.34, None, radius_km=10.0)
    assert not is_far
    assert km == 0.0


def test_place_far_km_thresholds():
    assert place_far_km(None) == 300.0
    assert place_far_km(10.0) == 150.0            # max(3*10, 150)
    assert place_far_km(100.0) == 300.0           # max(3*100, 150)


def test_center_drift_km_threshold():
    assert center_drift_km(10.0) == 50.0          # max(30, 50)
    assert center_drift_km(50.0) == 150.0         # max(150, 50)


def test_haversine_sanity():
    # ~1600 km London-Noida-scale sanity check on the shared helper both features reuse.
    km = haversine_km(51.5072, -0.1276, 28.61, 77.34)
    assert 6000 < km < 7500
