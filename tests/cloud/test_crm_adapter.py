from webscraper.agent import crm_payload


def test_crm_payload_shapes():
    assert crm_payload("jobs") == {"action": "jobs"}
    assert crm_payload("claim", job_id=7) == {"action": "claim", "job_id": 7}
    assert crm_payload("progress", job_id=7, phase="scraping", progress={"scraped_count": 3}) == {
        "action": "progress", "job_id": 7, "phase": "scraping", "progress": {"scraped_count": 3}}
    assert crm_payload("sync", job_id=7, rows=[{"place_key": "a"}]) == {
        "action": "sync", "job_id": 7, "rows": [{"place_key": "a"}]}
    assert crm_payload("done", job_id=7, status="done", error=None) == {
        "action": "done", "job_id": 7, "status": "done", "error": None}
