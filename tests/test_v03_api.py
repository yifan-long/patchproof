from fastapi.testclient import TestClient

import patchproof.api as api


def test_task_create_with_provider_masks_api_key_and_persists_without_it(tmp_path, monkeypatch):
    import json as _json

    from patchproof.config import Settings
    from patchproof.storage import SQLiteStore

    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "provider.db"
    settings = Settings(repo_path=str(repo), database_path=str(database), allow_project_target=True)
    monkeypatch.setattr(api, "settings", settings)

    with TestClient(api.app) as client:
        response = client.post(
            "/tasks",
            json={
                "goal": "修复某个明显的 bug 并补充回归测试确保行为一致",
                "repo_path": str(repo),
                "provider": {
                    "base_url": "https://example.test/v1",
                    "model": "my-model",
                    "api_key": "sk-secret-123",
                    "transport": "openai-compatible",
                },
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["provider"]["api_key"] == "***configured***"
        assert "sk-secret-123" not in response.text
        task_id = data["id"]

        persisted = _json.loads(SQLiteStore(database).get_task(task_id)["provider_json"])
        assert persisted["api_key"] == "***configured***"
        assert "sk-secret-123" not in persisted["api_key"]
        assert persisted["base_url"] == "https://example.test/v1"
        assert persisted["model"] == "my-model"


def test_task_create_without_provider_stores_none(tmp_path, monkeypatch):
    from patchproof.config import Settings

    repo = tmp_path / "repo"
    repo.mkdir()
    settings = Settings(
        repo_path=str(repo),
        database_path=str(tmp_path / "nokey.db"),
        env_file_path=str(tmp_path / "no-provider.env"),
        allow_project_target=True,
    )
    monkeypatch.setattr(api, "settings", settings)

    with TestClient(api.app) as client:
        response = client.post(
            "/tasks",
            json={
                "goal": "修复某个明显的 bug 并补充回归测试确保行为一致",
                "repo_path": str(repo),
            },
        )
        assert response.status_code == 200
        assert response.json()["provider"] is None


def test_evaluation_api_exposes_suites_cases_and_honest_preflight():
    with TestClient(api.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert "api_key_configured" in health.json()["provider"]
        suites = client.get("/suites")
        assert suites.status_code == 200
        assert sum(item["cases"] for item in suites.json()["suites"]) == 12
        cases = client.get("/cases")
        assert cases.status_code == 200
        assert len(cases.json()) == 12
        preflight = client.get("/preflight")
        assert preflight.status_code == 200
        assert len(preflight.json()["cases"]) == 12
        assert preflight.json()["cases"][-1]["provenance_state"] == "unresolved"


def test_evaluation_trigger_requires_both_explicit_confirmations():
    with TestClient(api.app) as client:
        response = client.post("/runs", json={"confirm_real": False})
        assert response.status_code == 400
        response = client.post("/runs", json={"confirm_real": True, "include_public": True})
        assert response.status_code == 400


def test_evaluation_report_survives_api_restart(tmp_path, monkeypatch):
    from patchproof.config import Settings
    from patchproof.evidence.canonical import hash_json

    settings = Settings(
        repo_path=str(tmp_path),
        database_path=str(tmp_path / "reports.db"),
        env_file_path=str(tmp_path / "missing.env"),
        allow_project_target=True,
    )
    monkeypatch.setattr(api, "settings", settings)
    report = {
        "schema_version": "patchproof.evaluation.v2",
        "evaluation_kind": "model_quality_comparison",
        "runs": [],
        "aggregate": {},
    }
    report_id = hash_json(report)[:16]

    with TestClient(api.app) as first_client:
        first_client.app.state.manager.store.save_evaluation_report(report_id, report)
        assert first_client.get(f"/reports/{report_id}").json() == report

    with TestClient(api.app) as restarted_client:
        assert restarted_client.get(f"/reports/{report_id}").json() == report
        assert restarted_client.get("/reports").json()[0]["report_id"] == report_id
