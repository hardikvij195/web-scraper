from _agent import is_verified, partition_new


def test_verified_needs_contact_and_enrichment():
    assert is_verified({"enrich_status": "done", "phone": "+911234", "email": None})
    assert is_verified({"enrich_status": "no_website", "phone": None, "email": "a@b.c"})
    assert not is_verified({"enrich_status": "pending", "phone": "+911234"})
    assert not is_verified({"enrich_status": "done", "phone": "", "email": ""})
    assert not is_verified({"enrich_status": None, "phone": "+911234"})


def test_partition_new_splits_on_existing_keys():
    rows = [{"place_key": "a"}, {"place_key": "b"}, {"place_key": "c"}]
    new, old = partition_new(rows, {"b"})
    assert [r["place_key"] for r in new] == ["a", "c"]
    assert [r["place_key"] for r in old] == ["b"]
