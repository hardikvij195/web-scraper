from webscraper.agent import DEVICE_NAME, crm_payload


def test_crm_payload_shapes():
    # Every payload now carries `device` (the machine label) for the heartbeat + job
    # targeting. Assert the rest of the shape and that device is always present.
    d = DEVICE_NAME
    assert crm_payload("jobs") == {"action": "jobs", "device": d}
    assert crm_payload("claim", job_id=7) == {"action": "claim", "device": d, "job_id": 7}
    assert crm_payload("progress", job_id=7, phase="scraping", progress={"scraped_count": 3}) == {
        "action": "progress", "device": d, "job_id": 7, "phase": "scraping", "progress": {"scraped_count": 3}}
    assert crm_payload("sync", job_id=7, rows=[{"place_key": "a"}]) == {
        "action": "sync", "device": d, "job_id": 7, "rows": [{"place_key": "a"}]}
    assert crm_payload("done", job_id=7, status="done", error=None) == {
        "action": "done", "device": d, "job_id": 7, "status": "done", "error": None}
