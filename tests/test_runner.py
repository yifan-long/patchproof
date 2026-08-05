import asyncio
import hashlib
from pathlib import Path

from patchproof.config import Settings
from patchproof.llm import FakeLLM
from patchproof.manager import TaskManager
from patchproof.models import TaskStatus
from patchproof.receipt import verify_receipt, verify_receipt_file
from patchproof.runner import AgentRunner
from patchproof.storage import SQLiteStore


def _manager(tmp_path: Path, actions: list[dict], *, max_steps: int = 10, max_invalid: int = 3):
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = Settings(
        repo_path=str(repo),
        database_path=str(tmp_path / "patchproof.db"),
        max_tool_steps=max_steps,
        max_invalid_actions=max_invalid,
        max_iterations=3,
        command_timeout_seconds=20,
        allow_project_target=True,
    )
    store = SQLiteStore(tmp_path / "patchproof.db")
    runner = AgentRunner(settings, store=store, llm=FakeLLM(actions))
    return TaskManager(settings, store=store, runner=runner), repo


def test_typed_loop_generates_receipt_and_can_apply(tmp_path: Path):
    async def flow():
        manager, repo = _manager(tmp_path, [])
        source = repo / "app.py"
        source.write_text("value = 1\n", encoding="utf-8")
        expected = "value = 2\n"
        actions = [
            {
                "tool": "apply_edit",
                "path": "app.py",
                "new_text": expected,
                "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            {"tool": "run_check", "argv": ["python", "-m", "compileall", "app.py"]},
            {"tool": "finish", "summary": "compile evidence is green", "verdict": "verified"},
        ]
        manager.runner.llm = FakeLLM(actions)
        record = await manager.create("change the value safely", str(repo), "python -m compileall app.py", 3, 10)
        await record.task

        assert record.status == TaskStatus.AWAITING_APPLY
        assert record.tool_calls == 3
        assert record.receipt is not None
        assert verify_receipt(record.receipt.receipt, record.receipt.receipt_hash)
        assert record.receipt.file_verified is True
        assert Path(record.receipt.artifact_path).is_file()
        pending_receipt_hash = record.receipt.receipt_hash
        assert manager.verify_chain(record.id)
        await manager.apply(record.id)
        assert record.status == TaskStatus.COMPLETED
        assert source.read_text(encoding="utf-8") == expected
        assert record.receipt.receipt["verdict"] == "applied"
        assert record.receipt.receipt_hash != pending_receipt_hash
        assert verify_receipt_file(record.receipt.artifact_path, record.receipt.file_sha256)

    asyncio.run(flow())


def test_dangerous_check_creates_persisted_approval_and_resumes(tmp_path: Path):
    async def flow():
        actions = [
            {"tool": "run_check", "argv": ["python", "-c", "print('approved')"]},
            {"tool": "finish", "summary": "approved check passed", "verdict": "verified"},
        ]
        manager, repo = _manager(tmp_path, actions)
        record = await manager.create("run an approved local check", str(repo), "python -c print('approved')", 3, 10)
        for _ in range(40):
            if record.status == TaskStatus.AWAITING_COMMAND_APPROVAL:
                break
            await asyncio.sleep(0.01)
        assert record.status == TaskStatus.AWAITING_COMMAND_APPROVAL
        approval_id = record.pending_approval_id
        await manager.approve_command(record.id, True, approval_id)
        await record.task

        assert record.status == TaskStatus.AWAITING_APPLY
        assert record.approvals[0].approved is True
        assert any(event.stage == "approval_resolved" for event in record.events)
        assert manager.verify_chain(record.id)

    asyncio.run(flow())


def test_arbitrary_successful_check_cannot_authorize_finish(tmp_path: Path):
    async def flow():
        manager, repo = _manager(
            tmp_path,
            [
                {"tool": "run_check", "argv": ["python", "--version"]},
                {"tool": "finish", "summary": "wrong evidence", "verdict": "verified"},
            ],
            max_steps=3,
        )
        (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
        record = await manager.create("reject arbitrary check evidence", str(repo), "python -m pytest -q", 3, 3)
        await record.task
        assert record.status == TaskStatus.FAILED
        assert record.failure_category == "budget_exhausted"
        assert record.required_check_verified is False
        assert record.receipt is None
        assert any("required check" in event.message or event.stage == "observation" for event in record.events)

    asyncio.run(flow())


def test_edit_invalidates_required_check_evidence(tmp_path: Path):
    async def flow():
        manager, repo = _manager(tmp_path, [], max_steps=6)
        source = repo / "app.py"
        source.write_text("value = 1\n", encoding="utf-8")
        second = "value = 2\n"
        actions = [
            {
                "tool": "apply_edit",
                "path": "app.py",
                "new_text": second,
                "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            {"tool": "run_check", "argv": ["python", "-m", "compileall", "app.py"]},
            {
                "tool": "apply_edit",
                "path": "app.py",
                "new_text": "value = 3\n",
                "expected_sha256": hashlib.sha256(second.encode("utf-8")).hexdigest(),
            },
            {"tool": "finish", "summary": "stale evidence", "verdict": "verified"},
        ]
        manager.runner.llm = FakeLLM(actions)
        record = await manager.create("invalidate check after edit", str(repo), "python -m compileall app.py", 3, 6)
        await record.task
        assert record.status == TaskStatus.FAILED
        assert record.required_check_verified is False
        assert record.required_check_evidence_generation is None
        assert record.edit_generation == 2

    asyncio.run(flow())


def test_invalid_action_limit_is_a_real_failure_category(tmp_path: Path):
    async def flow():
        manager, repo = _manager(
            tmp_path,
            [{"tool": "run_shell", "command": "rm -rf ."}] * 3,
            max_steps=10,
            max_invalid=2,
        )
        record = await manager.create("exercise invalid action handling", str(repo), "python -m compileall .", 3, 10)
        await record.task
        assert record.status == TaskStatus.FAILED
        assert record.failure_category == "invalid_action_limit"
        assert record.invalid_actions == 2

    asyncio.run(flow())


def test_tool_budget_terminates_without_fake_completion(tmp_path: Path):
    async def flow():
        manager, repo = _manager(tmp_path, [{"tool": "inspect_diff"}] * 10, max_steps=2)
        record = await manager.create("stop after bounded tool budget", str(repo), "python -m compileall .", 3, 2)
        await record.task
        assert record.status == TaskStatus.FAILED
        assert record.failure_category == "budget_exhausted"
        assert record.receipt is None

    asyncio.run(flow())


def test_failed_check_can_drive_repair_observation(tmp_path: Path):
    async def flow():
        manager, repo = _manager(tmp_path, [])
        source = repo / "app.py"
        source.write_text("value =\n", encoding="utf-8")
        fixed = "value = 3\n"
        actions = [
            {"tool": "run_check", "argv": ["python", "-m", "compileall", "app.py"]},
            {
                "tool": "apply_edit",
                "path": "app.py",
                "new_text": fixed,
                "expected_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
            {"tool": "run_check", "argv": ["python", "-m", "compileall", "app.py"]},
            {"tool": "finish", "summary": "repaired", "verdict": "verified"},
        ]
        manager.runner.llm = FakeLLM(actions)
        record = await manager.create("repair syntax and verify", str(repo), "python -m compileall app.py", 3, 10)
        await record.task
        assert record.status == TaskStatus.AWAITING_APPLY
        assert record.iteration == 2
        assert any(event.stage == "repairing" for event in record.events)

    asyncio.run(flow())
