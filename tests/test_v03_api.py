from fastapi.testclient import TestClient

import patchproof.api as api


def test_evaluation_api_exposes_suites_cases_and_honest_preflight():
    with TestClient(api.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert "api_key_configured" in health.json()["provider"]
        suites = client.get("/suites")
        assert suites.status_code == 200
        assert sum(item["cases"] for item in suites.json()["suites"]) == 13
        cases = client.get("/cases")
        assert cases.status_code == 200
        assert len(cases.json()) == 13
        preflight = client.get("/preflight")
        assert preflight.status_code == 200
        assert len(preflight.json()["cases"]) == 13
        assert preflight.json()["cases"][-1]["provenance_state"] == "unresolved"


def test_evaluation_trigger_requires_both_explicit_confirmations():
    with TestClient(api.app) as client:
        response = client.post("/runs", json={"confirm_real": False})
        assert response.status_code == 400
        response = client.post("/runs", json={"confirm_real": True, "include_public": True})
        assert response.status_code == 400
