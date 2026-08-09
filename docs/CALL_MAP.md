# PatchProof 调用链速查（Call Map）

> 纯文字版。给每个关键函数列出 **"谁调我"（callers）** 和 **"我调谁"（callees）**。
> 配合 [CODE_GUIDE.md](CODE_GUIDE.md) 用：先在这里看到"数据从哪来、往哪去"，
> 再回 CODE_GUIDE 看"为什么这么设计"。
>
> 约定：`X → Y` 表示 X 调用了 Y；`X ← Y` 表示 X 被 Y 调用。

---

## 0. 分层总览（从外向里）

程序分 5 层，请求从外往里走，证据从里往外写：

1. **HTTP 层** `api/` —— 把内部能力暴露成 REST/SSE。
2. **生命周期层** `task/service.py` —— 任务状态机、审批、Apply 的"导演"。
3. **核心循环层** `task/runner.py` —— 模型→动作→观察的"演员"。
4. **能力层** `workspace/` `policy/` `llm/` `agent/` `index/` —— runner 的"手脚"。
5. **证据层** `infrastructure/sqlite.py` `receipt/` `evidence/` —— 所有事实的"账本"。

下面按这个顺序展开，最后在 §5 用一条端到端链把它们串起来。

---

## 1. HTTP 层 `api/`

### 1.1 api/app.py

- **谁调我**：`uvicorn patchproof.api:app`（启动命令）；`api/__init__.py` re-export `app`。
- **我调谁**：
  - 导入阶段：`api/tasks.py::router`、`api/evaluation.py::router`（挂路由）、`config::settings`（CORS）。
  - `lifespan(app)`：`TaskManager(settings)` → `store.close()`。
- **关键点**：`lifespan` 从 `patchproof.api` 包属性读 settings，测试可 monkeypatch。

### 1.2 api/tasks.py（任务路由）

| 路由 | 函数 | 我调谁 | 返回 |
|---|---|---|---|
| `GET /tasks` | `list_tasks` | `manager.list()` → 每个 record `.snapshot(chain_head=store.chain_head(id))` | 快照列表 |
| `POST /tasks` | `create_task` | `manager.create(...)` | TaskSnapshot |
| `GET /tasks/{id}` | `get_task` | `manager.get(id)` | TaskSnapshot |
| `GET /tasks/{id}/diff` | `get_diff` | `record.diff / changed_files` + `hash_text(diff)` | diff + 哈希 |
| `GET /tasks/{id}/receipt` | `get_receipt` | `manager.store.get_receipt(id)` → `_receipt_payload` | receipt + 校验结果 |
| `GET /tasks/{id}/receipt/verify` | `verify_task_receipt` | `manager.verify_chain(id)` + `_receipt_payload` | 逻辑/文件/链三校验 |
| `GET /tasks/{id}/events/verify` | `verify_task_events` | `manager.verify_chain(id)` + `store.chain_head(id)` | 链是否完整 |
| `POST /tasks/{id}/approve-command` | `approve_command` | `manager.approve_command(id, approved, approval_id)` | TaskSnapshot |
| `POST /tasks/{id}/apply` | `apply_task` | `manager.apply(id)` | TaskSnapshot |
| `POST /tasks/{id}/cancel` | `cancel_task` | `manager.cancel(id)` | TaskSnapshot |
| `GET /tasks/{id}/stream` | `stream_task` | 轮询 `store.get_events(id, after=cursor)`；`manager.get(id)` 判终态 | SSE 事件流 |
| `GET /benchmarks/runs` | `benchmark_runs` | `manager.store.list_benchmark_runs()` | 评测运行列表 |

- **谁调我**：前端 Vue 控制台；`api/app.py`（挂载 router）。
- **关键点**：SSE 用游标（`last-event-id` / `after`）增量拉事件，断线可续传。

### 1.3 api/evaluation.py（评测路由）

| 路由 | 函数 | 我调谁 |
|---|---|---|
| `GET /health` | `health` | 读 `settings.*` 拼 JSON |
| `GET /suites` | `list_suites` | `api/common._corpus_cases()` |
| `GET /cases` | `list_cases` | `_corpus_cases()` |
| `GET /preflight` | `evaluation_preflight` | `EvaluationOrchestrator(_APP_ROOT).preflight(cases, settings)` |
| `POST /runs` | `trigger_evaluation` | `EvaluationOrchestrator(_APP_ROOT).run(...)` → `store.save_evaluation_report(report_id, report)` |
| `GET /reports` | `list_reports` | `store.list_evaluation_reports()` |
| `GET /reports/{id}` | `get_report` | `store.get_evaluation_report(id)` |

### 1.4 api/common.py

- **谁调我**：`api/evaluation.py`（`_APP_ROOT`、`EvaluationRunRequest`、`_corpus_cases` 等）。
- **我调谁**：`corpus::load_cases`（加载 manifest）。
- **关键点**：`_APP_ROOT = config::PATCHPROOF_ROOT`；路径守卫 `_project_data_path` /
  `_project_manifest_path` 防止任意文件读写。

---

## 2. 生命周期层 task/service.py（TaskManager）

### 2.1 构造与恢复

- **谁调我**：`api/app.py::lifespan`（`TaskManager(settings)`）。
- **我调谁**：
  - `SQLiteStore(settings.database_path_resolved)`
  - `store.recover_running_tasks()` ← 把上次遗留 running 态改 `interrupted`
  - `store.list_tasks()` → 每个 `_from_row` 恢复成 `TaskRecord`
  - `AgentRunner(settings, store=self.store)`

### 2.2 create —— 建任务

- **谁调我**：`api/tasks.py::create_task`。
- **我调谁**：
  - `_validate_repo(repo_path)` → 校验目录存在且不是 PatchProof 自身
  - `policy::normalize_command(check_command)` → 固化成 `required_check_argv`
  - 构造 `TaskRecord` → `store.create_task(...)`（**先落库**）
  - `_emit(record, "queued", ...)` → 写事件 + `_persist`
  - `asyncio.create_task(_run(record))` → 后台推进
- **返回**：`TaskRecord`。

### 2.3 _run —— 后台驱动

- **谁调我**：`create` 里的 `asyncio.create_task`。
- **我调谁**：`runner.run(record, emit_callback)`；退出后：
  - 非终态 → 标 `FAILED_RECOVERABLE` + `_emit`
  - `record.clear_provider_key()`（API key 只驻内存）
  - `_persist(record)`
- **关键点**：`emit_callback` 由 `_emit` 提供，runner 每走一步就把事件写进链。

### 2.4 _emit / _persist —— 状态落库

- **_emit(record, stage, message, data)**：
  - **谁调我**：runner 的 emit 回调、`create`、`approve_command`、`cancel`、`apply`。
  - **我调谁**：`store.append_event(...)`（写链式事件）→ `record.events.append` → `_persist`。
- **_persist(record)**：
  - **我调谁**：`store.update_task(...)`（把内存态字段整体刷回 SQLite）。

### 2.5 审批

- **_create_approval(record, decision)**：
  - **谁调我**：`runner::_run_check`（当 `classify_argv` 判定需审批时调 `record.request_approval`）。
  - **我调谁**：`store.create_approval(...)`；设置 `record.approval_waiters[approval.id] = asyncio.Event()`。
- **approve_command(task_id, approved, approval_id)**：
  - **谁调我**：`api/tasks.py::approve_command`。
  - **我调谁**：`store.get_approval` → `_emit("approval_resolved")` → `store.resolve_approval(...)` → `waiter.set()`（唤醒 runner）。

### 2.6 apply —— 人工写回

- **谁调我**：`api/tasks.py::apply_task`。
- **我调谁**：
  - `workspace::open_workspace(original, staging, kind, max_file_bytes)`
  - `workspace.open_existing()` → `workspace.apply()`（内部复核源未变 + 原子写回）
  - `_emit(completed)` → `_seal_and_store_receipt(record, receipt)`（重密封 verdict=applied）
  - 可选 `workspace.cleanup()`
- **关键点**：`workspace.apply()` 里 `_assert_original_unchanged` 复核源仓库 HEAD/工作树/manifest。

### 2.7 其他

- `get` / `list`：从 `records` 字典或 `_from_row`（落库恢复）拿 `TaskRecord`；`get` 会刷新 receipt 校验。
- `cancel`：`cancel_event.set()` → 标 CANCELLED → `task.cancel()`。
- `verify_chain`：`store.verify_chain(id)`。
- `_seal_and_store_receipt`：`receipt::seal_receipt` → `write_receipt_atomic` → `store.save_artifact` → `store.save_receipt`。

---

## 3. 核心循环层 task/runner.py（AgentRunner）★

### 3.1 run —— 主入口

- **谁调我**：`task/service.py::_run`。
- **我调谁**（按顺序）：
  1. `workspace::select_workspace(original, staging, max_file_bytes, allow_git_worktree)` → `workspace.create()`
  2. `index::RepoIndex.build(staging)` → `index.context_for(goal)` / `source_context(goal)`
  3. `self._llm_for(record)` → `llm.plan(goal, index_context, source_context, check_command)`
  4. 主循环：`llm.next_action(...)` → `agent::parse_tool_action(raw)` → `self._dispatch(...)`
     → `observations.append(result)` → `workspace.diff()` → `self._persist_artifact_stats(...)`
  5. `FinishAction` 且 ok → `self._create_receipt(...)` → `emit(awaiting_apply)` → `return`
- **异常处理**：`asyncio.CancelledError` → CANCELLED；`LLMUnavailableError` → FAILED(llm_unavailable)；
  `BudgetExceeded` → FAILED(llm_budget_exhausted)；`AgentFailure` → FAILED(类别)；其余 → FAILED(runner_error)。

### 3.2 _dispatch —— 动作分发

- **谁调我**：`run` 主循环。
- **我调谁**（按 isinstance 分支）：
  - `SearchRepoAction` → `workspace.search_repo(query, max_results)`
  - `ReadFileAction` → `workspace.read_file(path, start, end)`
  - `ApplyEditAction` → `workspace.apply_edit(...)`，成功后：
    - `record.edit_generation += 1`
    - `record.required_check_verified = False`、`evidence_generation = None`（**旧验证作废**）
  - `InspectDiffAction` → `workspace.diff()`
  - `RunCheckAction` → `self._run_check(...)`
  - `FinishAction` → 校验 `record.required_check_evidence_valid`（门禁），通过才返回 ok
- **返回**：`(observation_dict, last_test_passed)`。

### 3.3 _run_check —— 检查命令

- **谁调我**：`_dispatch`（RunCheckAction）。
- **我调谁**：
  - `policy::parse_command(argv)` → `policy::classify_argv(argv)`
  - 需审批 → `record.request_approval(decision)` → `emit(awaiting_command_approval)` → `record.wait_for_approval(id)`
  - `self.executor.run(spec, cwd=workspace.staging, timeout_seconds, cancel_event)`（进程或 Docker）
  - 成功后按 argv 精确匹配更新完成资格（见 CODE_GUIDE §3.12）
- **返回**：result 字典。

### 3.4 _create_receipt —— 密封完成证据

- **谁调我**：`run`（finish 门禁通过后）。
- **我调谁**：
  - `receipt::build_patch_receipt(...)`（打包所有事实）
  - `receipt::write_receipt_atomic(task_id, receipt)` → 返回 `(path, file_hash)`
  - `store.save_artifact(...)`（存文件路径 + 字节哈希）
  - `store.save_receipt(...)`（存 receipt 记录）→ `verify_receipt(...)`

---

## 4. 能力层

### 4.1 workspace/strategies.py —— 隔离与写回

| 函数 | 谁调我 | 我调谁 |
|---|---|---|
| `select_workspace` | `runner.run` | `GitWorktreeWorkspace.inspect` → 选 worktree 或 `SnapshotWorkspace` |
| `open_workspace` | `service.apply` | 按 kind 构造对应工作区 |
| `GitWorktreeWorkspace.inspect` | `select_workspace` | `git rev-parse --show-toplevel` / `git status` / `git rev-parse HEAD` |
| `GitWorktreeWorkspace.create` | `runner.run` | `git worktree add --detach` |
| `SnapshotWorkspace.create` | `runner.run` | `shutil.copytree(original, staging, ignore=COPY_IGNORE)` |
| `_WorkspaceTextMixin.apply_edit` | `runner._dispatch` | `_relative`（边界）→ `_is_protected` → 前置 hash/old_text 校验 → `_write_text` |
| `_WorkspaceTextMixin.diff` | `runner` / `api.get_diff` | `_manifest(original)` vs `_manifest(staging)` + `difflib.unified_diff` |
| `_WorkspaceTextMixin._atomic_writeback` | `workspace.apply` | 临时写 + `os.replace` + fsync；失败用 backups 回滚 |

### 4.2 policy/commands.py —— 命令门禁与执行

| 函数 | 谁调我 | 我调谁 |
|---|---|---|
| `normalize_command` | `service.create` | `parse_command` → argv |
| `classify_argv` | `runner._run_check`、`faults` | `parse_command` → `_safe_readonly_check` / `HIGH_RISK` / `NETWORK_RISK` → `CommandDecision` |
| `ProcessExecutor.run` | `runner._run_check` | `_resolve_python_for_current_environment` → `asyncio.create_subprocess_exec` → `_decode_and_trim` |

### 4.3 agent/tools.py —— typed 动作解析

| 函数 | 谁调我 | 我调谁 |
|---|---|---|
| `parse_tool_action` | `runner.run` | `TOOL_ACTION_ADAPTER.validate_python(payload)` |
| `tool_catalog` | `llm.next_action`（拼 prompt） | — |
| `observation(tool, ok, data, error)` | `runner._dispatch` | — |

### 4.4 llm/client.py —— 模型适配

| 函数 | 谁调我 | 我调谁 |
|---|---|---|
| `LLMClient.json(system, prompt)` | `plan` / `next_action` / `one_shot` | `ledger.reserve` → `_request` → `ledger.commit` → 解析 JSON |
| `LLMClient.plan` | `runner.run` | `self.json(...)` |
| `LLMClient.next_action` | `runner.run` 主循环 | `self.json(...)`（prompt 里带工具目录 + 最近观察） |
| `LLMClient.one_shot` | `evals` baseline | `self.json(...)` |
| `FakeLLM.*` | 测试 / smoke | `ledger.reserve/commit` |

### 4.5 index/repo_index.py —— 静态索引

| 函数 | 谁调我 | 我调谁 |
|---|---|---|
| `RepoIndex.build` | `runner.run`、`evals/orchestrator` | `ast.parse` 每个 .py |
| `RepoIndex.context_for` | `runner.run` | `_tokens(goal)` → 按词频排符号 |
| `RepoIndex.source_context` | `runner.run`、`evals/orchestrator` | `_tokens` + focus_paths → 拼文件内容 |

---

## 5. 证据层

### 5.1 infrastructure/sqlite.py（SQLiteStore）

| 方法 | 谁调我 |
|---|---|
| `recover_running_tasks` | `service.__init__` |
| `create_task` / `update_task` / `get_task` / `list_tasks` | `service` |
| `append_event` | `service._emit`（runner 每步动作都经过它） |
| `get_events` / `chain_head` | `api/tasks.py`（SSE、receipt 事件链头） |
| `verify_chain` | `service.verify_chain` → `api/tasks.py` |
| `create_approval` / `resolve_approval` / `get_approval(s)` | `service` |
| `save_artifact` / `get_artifacts` | `runner._create_receipt`、`service._seal_and_store_receipt` |
| `save_receipt` / `get_receipt` | `runner`、`service`、`api/tasks.py` |
| `create/finish/list_benchmark_run` | `evals/benchmark` |
| `save/get/list_evaluation_report` | `api/evaluation.py` |

### 5.2 receipt/sealer.py

| 函数 | 谁调我 |
|---|---|
| `seal_receipt` | `service._seal_and_store_receipt`（内部 `compute_receipt_hash` + `_artifact_material_hash`） |
| `write_receipt_atomic` | `runner._create_receipt`、`service._seal_and_store_receipt` |
| `verify_receipt` | `service`、`api/tasks.py` |
| `verify_receipt_file` | `sqlite.save_receipt`/`get_receipt`（校验文件字节） |
| `build_patch_receipt` | `runner._create_receipt`、`faults` |

### 5.3 evidence/canonical.py

| 函数 | 谁调我 |
|---|---|
| `canonical_json` | 全系统（事件、receipt、报告） |
| `hash_bytes` / `hash_text` / `hash_json` | sqlite、receipt、api、evals |

---

## 6. 配置层 config/settings.py

| 成员 | 谁调我 |
|---|---|
| `Settings()` 构造 | `api/app.py`、`evals`、`tests` |
| 全局单例 `settings` | 几乎所有模块（`from ..config import settings`） |
| `repo_path_resolved` / `database_path_resolved` | `service.create`、`TaskManager.__init__` |
| `resolved_provider` / `resolved_transport` / `resolved_base_url` | `llm/client.py` |
| `provider_metadata` | `/health`、preflight、日志 |

---

## 7. 评测与溯源链（了解即可）

### 7.1 evals/orchestrator.py（EvaluationOrchestrator）

- **谁调我**：`api/evaluation.py`（preflight/run）、`evals/benchmark.py` CLI。
- **我调谁**：
  - `preflight`：`build_fetch_plan` + `docker.preflight()`
  - `run`：校验确认项 → `BudgetLedger` → 对每个 case：`_resolve_case_source` → `_run_pair`
  - `_run_pair`：`copytree_without_oracles` 复制两份 → `_initial_failure_gate`（两份必须一致失败）
    → baseline（`_run_real_baseline`）与 harness（`_run_real_harness`）
  - `_initial_failure_gate`：`_run_initial_check` ×2 → `RepoIndex.build` → `index.source_context`
- **产出**：成对评测报告 + 追加式 JSONL。

### 7.2 evals/benchmark.py（BenchmarkHarness + CLI）

- **谁调我**：`python -m patchproof.benchmark smoke|real|preflight|resolve-public|build-evaluator-image|faults`。
- **我调谁**：
  - `run_deterministic_smoke`：`_run_initial_fixture_check` → `_run_baseline`（oracle 编辑）→ `_run_harness`（FakeLLM 走完整闭环）
  - `run_real`：`EvaluationOrchestrator` 同源执行 baseline/harness
  - `_run_harness`：`SQLiteStore` + `AgentRunner(llm=FakeLLM)` + `TaskManager` → 走真实闭环

### 7.3 corpus/loader.py + public_resolver.py

- `load_cases`：读 manifest → `BenchmarkCase.model_validate`。
- `build_fetch_plan` / `execute_fetch_plan`：内容寻址的拉取（先规划，确认后才下载）。
- `PublicProvenanceResolver.resolve`：官方元数据 → 不可变 commit → `_probe_case`（在固定镜像里验证失败）。

### 7.4 docker/executor.py + evaluator_image.py

- `DockerEvalExecutor.run`：`build_run_argv`（`--read-only --network none` 等硬隔离）→ `preflight` → 执行。
- `DockerProcessAdapter.run`：把 Docker 结果包装成 `policy::ExecutionResult` 形状，供 runner 直接用。
- `EvaluatorImageBuilder.build`：构建 + inspect + 运行时探测 → 写 lock 文件。

### 7.5 faults/scenarios.py

- `FaultRunner.run_all`：逐个跑 12 个场景 hook，校验"期望状态 == 实际状态"。

---

## 8. 一条端到端调用链（把上面全串起来）

```
用户前端
  │ POST /tasks
  ▼
api/tasks.py::create_task
  └→ service::TaskManager.create
        ├→ policy::normalize_command      （验收命令→argv）
        ├→ store.create_task              （先落库 status=queued）
        └→ asyncio.create_task(_run)
              └→ runner::AgentRunner.run
                    ├→ workspace::select_workspace → workspace.create   （隔离）
                    ├→ index::RepoIndex.build → context_for/source_context（侦察）
                    ├→ llm::LLMClient.plan → llm::LLMClient.json       （计划）
                    │      └→ budget::BudgetLedger.reserve/commit       （预算）
                    └→ for step in max_steps:                          （核心循环）
                          ├→ llm::next_action → agent::parse_tool_action（模型输出→动作）
                          ├→ runner::_dispatch
                          │     ├→ workspace::apply_edit → _relative/_is_protected（编辑，edit_generation+1）
                          │     ├→ runner::_run_check → policy::classify_argv → ProcessExecutor.run（检查）
                          │     │        └（需审批）service::_create_approval → api 批准 → waiter.set()
                          │     └→ runner::_create_receipt → receipt::build_patch_receipt/seal → write_receipt_atomic
                          │              → store::save_artifact + save_receipt
                          ├→ store::append_event                        （每步动作→事件链）
                          └→ emit → service::_emit → store::update_task （状态落库）
                    └→ 停在 awaiting_apply（SSE 通知前端）

用户审 diff
  │ POST /tasks/{id}/apply
  ▼
api/tasks.py::apply_task
  └→ service::TaskManager.apply
        ├→ workspace::open_workspace → open_existing → apply
        │      └→ _assert_original_unchanged（复核源 HEAD/工作树/manifest）→ _atomic_writeback
        ├→ store::append_event（completed）
        └→ service::_seal_and_store_receipt（重密封 verdict=applied）
```

**一句话收尾**：从 HTTP 进来的是"目标+验收命令"，往里走每一层都只做一件事（管契约、管隔离、
管安全、管证据），最终输出的不是"一段解释"，而是 **diff + 事件链 + Patch Receipt** 三件可复核的证据。
