from pathlib import Path

from fastapi.testclient import TestClient

import patchproof.api as api
from patchproof.config import Settings
from patchproof.models import TaskStatus
from patchproof.storage import SQLiteStore


def test_api_recovers_running_task_and_resumes_sse_from_cursor(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    database = tmp_path / "api.db"
    store = SQLiteStore(database)
    store.create_task(
        task_id="api-task",
        goal="recover api task",
        repo_path=str(repo),
        check_command="python -m compileall .",
        max_iterations=3,
        max_steps=10,
        status=TaskStatus.TESTING,
    )
    store.append_event("api-task", stage="testing", message="before restart", data={"cursor": 1})
    settings = Settings(repo_path=str(repo), database_path=str(database), allow_project_target=True)
    monkeypatch.setattr(api, "settings", settings)

    with TestClient(api.app) as client:
        task = client.get("/tasks/api-task")
        assert task.status_code == 200
        assert task.json()["status"] == TaskStatus.INTERRUPTED.value
        assert client.get("/tasks/api-task/events/verify").json()["verified"] is True
        stream = client.get("/tasks/api-task/stream?after=1")
        assert stream.status_code == 200
        assert "interrupted" in stream.text
        assert "id:" in stream.text

