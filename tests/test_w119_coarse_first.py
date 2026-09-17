"""W119 (CRM T765) — a small circle starts as one whole-circle tile; only saturated keywords are split."""
from webscraper.maps import COARSE_FIRST_MAX_KM, initial_tiles, keyword_bands, plan_steps, saturated_keywords

SYDNEY = (-33.8688, 151.2093)


def test_ten_km_circle_is_one_tile():
    centers, tile_km = initial_tiles(SYDNEY, 10.0)
    assert centers == [SYDNEY]
    assert tile_km == 10.0


def test_boundary_and_big_circle_keep_the_grid():
    centers, tile_km = initial_tiles(SYDNEY, COARSE_FIRST_MAX_KM)
    assert len(centers) == 1 and tile_km == COARSE_FIRST_MAX_KM
    centers, tile_km = initial_tiles(SYDNEY, 50.0)
    assert tile_km == 6.25
    assert len(centers) > 40          # the radius/8 grid, centre first
    assert centers[0] == SYDNEY


def test_plan_size_for_a_230_keyword_sector():
    queries = [f"kw {i}" for i in range(230)]
    centers, tile_km = initial_tiles(SYDNEY, 10.0)
    steps = plan_steps(keyword_bands(queries), centers, tile_km)
    assert len(steps) == 230          # was 37 tiles x 230 = 8,510 before W119


def test_only_full_feeds_are_split():
    band = ["cleaning company", "bird control", "office cleaning"]
    hits = {(SYDNEY, "cleaning company"): 104, (SYDNEY, "bird control"): 7, (SYDNEY, "office cleaning"): 100}
    assert saturated_keywords(band, hits, SYDNEY, 100) == ["cleaning company", "office cleaning"]
    assert saturated_keywords(band, hits, (0.0, 0.0), 100) == []      # another tile: nothing recorded
    assert saturated_keywords(band, {}, SYDNEY, 100) == []
