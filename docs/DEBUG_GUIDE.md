# PatchProof 调试指南（VSCode）

> 目的：不用读 9000 行代码，用**断点 + 观察变量**让程序自己把流程演给你看。
> 纯文字版，不含任何图。

---

## 1. 一次性配置（5 分钟）

### 1.1 选择正确的解释器

Ctrl+Shift+P → "Python: Select Interpreter" → 选：

```
<项目根>/.venv/Scripts/python.exe
```

（用 `uv sync` 装好的虚拟环境，否则断点命不中。）

### 1.2 建 `.vscode/launch.json`

在项目根建 `.vscode/launch.json`，贴入：

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Debug API server (uvicorn)",
      "type": "debugpy",
      "request": "launch",
      "module": "uvicorn",
      "args": ["patchproof.api:app", "--app-dir", "src", "--port", "8010"],
      "cwd": "${workspaceFolder}",
      "python": "${workspaceFolder}/.venv/Scripts/python.exe",
      "console": "integratedTerminal"
    },
    {
      "name": "Debug pytest (选中用例)",
      "type": "debugpy",
      "request": "launch",
      "module": "pytest",
      "args": ["tests/test_runner.py::test_xxx", "-s"],
      "cwd": "${workspaceFolder}",
      "python": "${workspaceFolder}/.venv/Scripts/python.exe",
      "console": "integratedTerminal"
    },
    {
      "name": "Debug benchmark smoke (全闭环, 免模型)",
      "type": "debugpy",
      "request": "launch",
      "module": "patchproof.benchmark",
      "args": [
        "smoke",
        "--manifest", "benchmarks/manifest.v2.json",
        "--project-root", ".",
        "--output", "data/benchmark-smoke.json"
      ],
      "cwd": "${workspaceFolder}",
      "python": "${workspaceFolder}/.venv/Scripts/python.exe",
      "console": "integratedTerminal"
    },
    {
      "name": "Debug faults (故障注入)",
      "type": "debugpy",
      "request": "launch",
      "module": "patchproof.faults",
      "args": ["run"],
      "cwd": "${workspaceFolder}",
      "python": "${workspaceFolder}/.venv/Scripts/python.exe",
      "console": "integratedTerminal"
    }
  ]
}
```

> 说明：
> - `python` 直接指向 `.venv/Scripts/python.exe`，避免 VSCode 猜错解释器。
> - **不要加 `--reload`**：Windows 下 reload worker 可能遗留 SelectorEventLoop，
>   导致 `asyncio.create_subprocess_exec` 抛 `NotImplementedError`。项目已在
>   `api/app.py` 里显式设了 Proactor，正常启动即可。
> - 需要跟进 pydantic/fastapi 库内部时，给对应配置加 `"justMyCode": false`。

---

## 2. 推荐断点：按流程阶段放，一次看一段

> 用法：放好下面某阶段的所有断点 → 跑对应启动配置 → 用 **Step Over (F10)** 走，
> 在 **Watch / Variables** 里盯指定的变量。不要一次全放，分阶段看。

### 阶段 0 · 启动与配置

| 断点位置 | 看什么 |
|---|---|
| `config/settings.py::Settings.model_post_init` | `self.anthropic_model`、`selected_inputs`（配置优先级链到底从哪取的） |
| `task/service.py::TaskManager.__init__` | `store.db_path`、`recover_running_tasks()` 返回的 ids |

启动配置：**Debug API server**，或直接跑任意 pytest。

### 阶段 1 · 创建任务

| 断点位置 | 看什么 |
|---|---|
| `task/service.py::TaskManager.create` | `normalized_check_command`、`required_check_argv`、`task_id` |
| `task/service.py::_emit` | `stage`、`message`、`event.event_hash` |

启动配置：**Debug API server** → 打开 `http://localhost:8010/docs` → 调 `POST /tasks`。
（不填 provider 就不碰模型。）

### 阶段 2 · 隔离与侦察

| 断点位置 | 看什么 |
|---|---|
| `workspace/strategies.py::select_workspace` | `eligible`、返回的 `workspace.kind`（git_worktree / snapshot） |
| `index/repo_index.py::RepoIndex.build` | `len(index.files)`、`len(index.symbols)` |

### 阶段 3 · 工具循环（核心，最值得看）

| 断点位置 | 看什么 |
|---|---|
| `task/runner.py::run` 的 `for step in range(...)` 行 | `step`、`len(observations)` |
| `task/runner.py::_dispatch` 开头 | `action.tool`、`action` 全字段 |
| `task/runner.py::_dispatch` 里 `ApplyEditAction` 分支 | `record.edit_generation` 变化、`record.required_check_verified` 被清空 |
| `agent/tools.py::parse_tool_action` 的 except | `InvalidToolAction`（模型输出不合法时的正常路径） |
| `workspace/strategies.py::_WorkspaceTextMixin.apply_edit` | `current_hash`、`expected_sha256`、`matches`（old_text 唯一性） |

**最快看到这阶段**：跑 **Debug benchmark smoke**（用 FakeLLM，不联网，几秒走完整个循环）。

### 阶段 4 · 检查与完成资格

| 断点位置 | 看什么 |
|---|---|
| `task/runner.py::_run_check` | `decision.requires_approval`、`normalized_argv == record.required_check_argv`、`result.returncode` |
| `policy/commands.py::classify_argv` | 返回的 `CommandDecision.allowed/requires_approval/risk_level` |
| `task/runner.py::_dispatch` 的 `FinishAction` 分支 | `record.required_check_evidence_valid`（完成资格开关） |

### 阶段 5 · 证据固化

| 断点位置 | 看什么 |
|---|---|
| `task/runner.py::_create_receipt` | `build_patch_receipt(...)` 的入参、`file_hash` |
| `receipt/sealer.py::seal_receipt` | `sealed["receipt_hash"]`、`sealed["artifact_sha256"]` |
| `infrastructure/sqlite.py::append_event` | `prev_hash`、`event_hash`（看链式哈希怎么串） |

### 阶段 6 · 人工裁决与写回

| 断点位置 | 看什么 |
|---|---|
| `task/service.py::TaskManager.apply` | `changed`（写回的文件列表） |
| `workspace/strategies.py::_atomic_writeback` | `backups` 字典（回滚准备） |

---

## 3. 三条"必看"调试路径（由浅入深）

### 路径 A：看懂核心循环（10 分钟，不碰模型）

1. 启动 **Debug benchmark smoke**；
2. 在 `runner.run` 主循环、`_dispatch`、`_run_check`、`_create_receipt` 四处放断点；
3. F5 跑，Step Over 走完一遍。

你会看到：隔离副本 → 计划 → apply_edit（edit_generation 变 1）→ run_check（通过，获得完成资格）
→ finish（门禁通过）→ 密封 Receipt → `awaiting_apply`。**这就是整个程序的核心闭环。**

### 路径 B：看懂"编辑使旧验证失效"（5 分钟）

1. 在 `_run_check` 里 `result.returncode == 0` 那行，和 `_dispatch` 的 `ApplyEditAction` 分支各放断点；
2. 观察顺序：先 run_check 成功（`required_check_evidence_generation == edit_generation`）→
   再 apply_edit（`edit_generation + 1`，`required_check_verified = False`）→
   这时 finish 会被拒（`required_check_evidence_valid == False`）。
3. 这就是"先蒙对检查、再乱改代码"被机制性排除的地方。

### 路径 C：看懂事件链与 Receipt（5 分钟）

1. 在 `append_event` 和 `seal_receipt` 放断点；
2. 观察 `prev_hash` → `event_hash` 逐条串联；Receipt 的 `receipt_hash` 不包含自身字段；
3. 跑完后打开 `data/runs/<task_id>/receipt.json`，对比 `infrastructure/sqlite.py::verify_chain`
   的逻辑——你可以在 sqlite 里手动改一条事件，再跑一次 verify 看它返回 False（故障注入
   `faults/scenarios.py::_run_event_tamper` 就是这么验证的）。

---

## 4. 常见问题

| 现象 | 原因 / 解法 |
|---|---|
| 断点没命中 | 解释器没选 `.venv`；或先 `uv sync` 一下 |
| `NotImplementedError` on subprocess | Windows 用了 reload / SelectorEventLoop；改回正常启动 |
| 想跟到 pydantic/fastapi 内部 | 该配置加 `"justMyCode": false` |
| 跑完整 pytest 太久 | `args` 里只留一个测试文件/用例 |
| 想边跑边看数据库 | 任务跑完看 `data/patchproof.db`（SQLite），Receipt 在 `data/runs/<task_id>/receipt.json` |
| 想断点在"模型返回了什么" | `llm/client.py::LLMClient.json` 的 `response` 处（真模型需配置 API key） |

---

## 5. 兜底：不依赖调试器也能观测

如果你不想用 VSCode 调试器，两个 CLI 就是最好的"可视化"：

```powershell
# 确定性全闭环（FakeLLM，免模型）——输出含完整证据结构
uv run python -m patchproof.benchmark smoke --manifest benchmarks/manifest.v2.json --project-root . --output data/benchmark-smoke.json

# 故障注入——passed:true 意味着所有安全不变量真的成立
uv run python -m patchproof.faults run --output data/fault-report.json
```

配合阅读顺序 [CODE_GUIDE.md](CODE_GUIDE.md) 第 2 节（7 阶段流程）+ 本节路径 A/B/C，
几小时内就能把整个程序"缕"清楚。
