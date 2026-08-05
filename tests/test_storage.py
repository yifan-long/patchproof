from pathlib import Path

from patchproof.models import TaskStatus
from patchproof.storage import SQLiteStore


def _task(store: SQLiteStore, task_id: str = "task-1") -> None:
    store.create_task(
        task_id=task_id,
        goal="test durable evidence",
        repo_path="C:/repo",
        check_command="python -m pytest -q",
        max_iterations=3,
        max_steps=10,
    )


def test_event_hash_chain_detects_tampering(tmp_path: Path):
    store = SQLiteStore(tmp_path / "patchproof.db")
    _task(store)
    first = store.append_event("task-1", stage="planning", message="plan", data={"step": 1})
    second = store.append_event("task-1", stage="tool_call", message="search", data={"query": "x"})

    assert first.prev_hash == "0" * 64
    assert second.prev_hash == first.event_hash
    assert store.verify_chain("task-1") is True

    with store.connection() as connection:
        connection.execute("UPDATE events SET message = ? WHERE task_id = ? AND seq = ?", ("tampered", "task-1", 2))
    assert store.verify_chain("task-1") is False


def test_running_tasks_are_recovered_as_interrupted(tmp_path: Path):
    database = tmp_path / "patchproof.db"
    first = SQLiteStore(database)
    _task(first)
    first.update_task("task-1", status=TaskStatus.TESTING.value, current_stage=TaskStatus.TESTING.value)
    first.append_event("task-1", stage="testing", message="running", data={})

    second = SQLiteStore(database)
    recovered = second.recover_running_tasks()
    row = second.get_task("task-1")

    assert recovered == ["task-1"]
    assert row is not None
    assert row["status"] == TaskStatus.INTERRUPTED.value
    assert second.get_events("task-1")[-1].stage == TaskStatus.INTERRUPTED.value
    assert second.verify_chain("task-1") is True


def test_approval_round_trip_is_durable(tmp_path: Path):
    store = SQLiteStore(tmp_path / "patchproof.db")
    _task(store)
    approval = store.create_approval(
        "task-1",
        kind="run_check",
        argv=["git", "status"],
        risk_level="medium",
        reason="not in safe fixture policy",
    )
    assert store.get_approvals("task-1")[0].approved is None
    resolved = store.resolve_approval(approval.id, True, event_seq=1)
    assert resolved.approved is True
    assert store.get_approval(approval.id).approved is True


def test_required_check_evidence_is_durable(tmp_path: Path):
    database = tmp_path / "required-check.db"
    store = SQLiteStore(database)
    store.create_task(
        task_id="required-task",
        goal="persist required check evidence",
        repo_path="C:/repo",
        check_command="python -m pytest -q",
        max_iterations=3,
        max_steps=10,
        required_check_argv=["python", "-m", "pytest", "-q"],
    )
    store.update_task(
        "required-task",
        required_check_verified=1,
        required_check_evidence_generation=2,
        edit_generation=2,
        required_check_last_result_json='{"returncode":0,"required_match":true}',
    )
    reopened = SQLiteStore(database)
    row = reopened.get_task("required-task")
    assert row is not None
    assert row["required_check_argv_json"] == '["python","-m","pytest","-q"]'
    assert row["required_check_verified"] == 1
    assert row["required_check_evidence_generation"] == 2
    assert row["edit_generation"] == 2
