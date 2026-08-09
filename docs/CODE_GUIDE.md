# PatchProof 代码阅读指南（文字版）

> 纯文字版，不依赖任何图表渲染。目标是：先把整个程序的流程用文字注解讲清楚，
> 再按流程顺序把每个模块的"做什么 / 怎么实现 / 为什么"讲透，最后把每个环节
> 串成一条能从头走到尾的完整主线。

---

## 1. 项目一句话

**PatchProof 不是让 AI 更会写代码，而是让 AI 写完代码之后结果有凭有据。**
它把模型放进隔离工作区，只开放 6 个类型化工具；模型每次动作、观察与审批都进入
SHA-256 事件链；只有"最后一次编辑之后、按任务指定的原始命令真实跑通"才允许生成
Patch Receipt；最终 diff 永远由人审阅，**系统永不自动 Apply**。

---

## 2. 全程序流程文字注解

下面把一个任务"从创建到写回真实仓库"的完整生命周期拆成 7 个阶段。每个阶段标注：
**入口在哪、经过哪些模块、发生什么、产出什么**。这是理解全程序的主干。

### 阶段 0 · 启动与配置

- **入口**：`uvicorn patchproof.api:app`（本地 `demo.cmd`、Linux 部署脚本都这样启动）。
- **经过的模块**：`api/app.py`、`config/settings.py`、`infrastructure/sqlite.py`。
- **发生的事**：
  1. `api/app.py` 构建 FastAPI app，挂载 `api/tasks.py` 与 `api/evaluation.py` 两组路由，
     加 CORS 中间件（允许的源来自 `settings.cors_origin_list`）；
  2. lifespan 启动时创建 `TaskManager(settings)`，退出时 `store.close()`；
  3. `TaskManager.__init__` 打开 `SQLiteStore`，并调用 `recover_running_tasks()`：把上一次
     进程遗留的 running 状态任务改成 `interrupted`；
  4. `settings` 是 `config/settings.py` 里的全局单例，配置来源按优先级链合并
     （见第 3 节模块详解）。
- **产出**：一个"进程重启也不丢事实"的持久化底座，一个能接受请求的 HTTP 服务。

### 阶段 1 · 创建任务（POST /tasks）

- **入口**：`api/tasks.py::create_task`。
- **经过的模块**：`api/tasks.py` → `task/service.py::TaskManager.create` → `infrastructure/sqlite.py`。
- **发生的事**：
  1. `_validate_repo` 校验目标目录存在、且不是 PatchProof 自身运行目录；
  2. `normalize_command(check_command)` 把验收命令解析成规范化 argv，存进
     `required_check_argv` —— 模型以后只能按这个 argv 验收；
  3. 构造内存态 `TaskRecord`，**先 `store.create_task` 落库（status=queued），再启动后台协程**；
  4. `asyncio.create_task(_run(record))` 开始执行。
- **产出**：一条已持久化的任务记录 + 一个正在推进的后台协程。

### 阶段 2 · 隔离与侦察

- **发生地**：`task/runner.py::run` 的开头。
- **经过的模块**：`workspace/strategies.py`、`index/repo_index.py`、`llm/client.py`。
- **发生的事**：
  1. `select_workspace(original, staging, ...)` 决定隔离策略：干净 Git 仓库 → detached
     worktree；否则 → 复制副本（snapshot）；然后 `workspace.create()`；
  2. `RepoIndex.build(staging)` 建静态 AST/行号索引；
  3. `llm.plan(goal, index_context, source_context, check_command)` 生成显式计划。
- **产出**：一个模型碰不到真实仓库的隔离副本 + 一份可审计的计划。

### 阶段 3 · 工具循环（核心）

- **发生地**：`run()` 里的 `for step in range(1, max_steps + 1)`。
- **经过的模块**：`llm/client.py` → `agent/tools.py` → `task/runner.py::_dispatch` → `workspace/strategies.py`。
- **每一步发生的事**：
  1. `llm.next_action(...)` 返回模型产出的原始 JSON；
  2. `parse_tool_action` 严格校验（`agent/tools.py`）；解析失败 → 记一条"受限 observation"
     并 `invalid_actions += 1`，超过 `max_invalid_actions` 就整体失败；
  3. `_dispatch` 按工具分发：
     - `search_repo` / `read_file` / `inspect_diff` → 直接读隔离工作区；
     - `apply_edit` → 走工作区的前置校验（hash/old_text），**成功后 `edit_generation += 1`，
       并把旧验证全部作废**；
     - `run_check` → 进策略门禁 + 执行器（阶段 4）；
     - `finish` → 走完成资格校验（阶段 4 的判定）；
  4. 结果 `observation` 追加进模型上下文、写事件链、刷新 diff 并持久化；
  5. 若 `finish` 通过 → 进入阶段 5（密封 Receipt）；若循环耗尽 → 如实判失败。
- **产出**：一串有序、可回放的动作/观察事件。

### 阶段 4 · 检查与完成资格

- **发生地**：`task/runner.py::_run_check` + `task/state.py::required_check_is_fresh`。
- **经过的模块**：`policy/commands.py`、`workspace/strategies.py`（执行目录）、`task/state.py`。
- **关键规则**：
  - `classify_argv` 把命令分成"只读白名单 / 需审批 / 高危"；需审批 → 状态机转
    `awaiting_command_approval`，挂起等 HTTP 批准（`api/tasks.py::approve_command`）；
  - 只有"argv 与任务创建时的 `required_check_argv` **完全一致** 且 returncode==0"，
    才写 `required_check_verified = True`、`evidence_generation = edit_generation`；
  - 此后任何 `apply_edit` 都让 `edit_generation` 前进，使旧证据失效（`required_check_is_fresh` 判否）。
- **产出**：一个"完成资格"开关 —— 只有它为真，`finish(verified)` 才可能通过。

### 阶段 5 · 证据固化（Patch Receipt）

- **发生地**：`task/runner.py::_create_receipt` → `receipt/sealer.py`。
- **经过的模块**：`receipt/sealer.py`、`infrastructure/sqlite.py`、`evidence/canonical.py`。
- **发生的事**：把计划摘要、工具统计、文件前后哈希、diff 哈希、命令记录、审批记录、
  测试证据、事件链头打包；`seal_receipt` 计算自校验哈希；`write_receipt_atomic` 原子写文件；
  `store.save_receipt` 落库；文件字节哈希单独存 SQLite。
- **产出**：一份可自校验的 Patch Receipt（内容被改 → 逻辑哈希对不上；文件被换/丢了 →
  字节哈希对不上，两个失败可区分）。

### 阶段 6 · 人工裁决与写回

- **入口**：`api/tasks.py::apply_task` → `task/service.py::TaskManager.apply`。
- **经过的模块**：`workspace/strategies.py`（写回）、`receipt/sealer.py`（重密封）。
- **发生的事**：
  1. 任务停在 `awaiting_apply`，SSE 通知前端，人审 diff 与 Receipt；
  2. `apply` → `workspace.apply()` 先复核源仓库的 HEAD / 工作树 / manifest **都没变**，
     再 `_atomic_writeback` 原子写回（每个文件临时写 + `os.replace` + fsync，失败自动回滚）；
  3. 状态转 `completed`，Receipt 重新密封 `verdict=applied`；
  4. 若配置了 `cleanup_workspaces`，再清理隔离副本。
- **产出**：真实仓库里安全写回的最终修改 + 一条走完整个生命周期的闭环证据。

---

## 3. 模块详解（按流程顺序）

> 每个模块标注**在流程中的角色**，再给"做什么 / 怎么实现 / 为什么"。

### 3.1 config/settings.py —— 流程第 0 阶段的配置源

- **角色**：全项目的配置与路径锚点。
- **做什么**：集中管理数据库路径、模型端点/模型名/API key、Docker 限制、评测预算；
  给出 `PATCHPROOF_ROOT`（仓库根）和 `PROJECT_ROOT`（仓库父目录）两个全局路径锚点。
- **怎么实现**：基于 `pydantic-settings` 的 `BaseSettings`，在 `model_post_init` 里用
  `choose()` 手动拼装优先级链；底部实例化全局单例 `settings = Settings()`。
- **为什么**：优先级固定为 **进程环境变量 > 显式构造参数 > env 文件 > 默认值**。
  这样测试 `Settings(**override)` 永远压得过 `.env`；API key 用 `repr=False` 且只驻内存，
  `provider_metadata` 只暴露非敏感视图。

### 3.2 api/app.py —— 流程第 0 阶段的 HTTP 装配

- **角色**：把各路由组装成可运行的服务。
- **做什么**：建 FastAPI app、加 CORS、挂路由；lifespan 里创建/关闭 TaskManager。
- **怎么实现**：lifespan 在启动时读 `patchproof.api.settings`（从包属性取，便于测试
  monkeypatch），创建 `TaskManager`；Windows 下显式设 Proactor 事件循环策略。
- **为什么**：Windows 的 uvicorn --reload 可能遗留 SelectorEventLoop，它不支持 subprocess，
  会让每次 run_check 抛 `NotImplementedError` —— 显式固定 Proactor 规避。

### 3.3 infrastructure/sqlite.py —— 贯穿全程的真相库与事件链

- **角色**：任务、事件、审批、Receipt、评测报告的**唯一持久化真相**。
- **做什么**：落库/读取所有状态；`append_event` 写链式事件；`verify_chain` 校验链完整性。
- **怎么实现**：每次操作一个短连接 + WAL；`append_event` 取上一条 `event_hash` 作 `prev_hash`，
  新哈希 = `sha256(prev_hash + 规范化 payload)`；`verify_chain` 逐条重算比对。
- **为什么**：短连接 + WAL 避免跨协程共享连接、减少写锁竞争；事件链的意义是"篡改任一条
  会破坏其后所有哈希"——SQLite 不是 write-once，但篡改可被检测。老库升级走
  `_ensure_task_columns` 的增量 `ALTER TABLE`，不丢历史。

### 3.4 task/models.py —— 贯穿全程的数据契约

- **角色**：定义状态机取值、API 快照、审批/事件结构，以及模型唯一能调的 6 种 typed action。
- **做什么**：`TaskStatus`（StrEnum，字符串值可直接落库）；`TERMINAL_STATUSES` /
  `RUNNING_STATUSES` 两组集合；`*Action` 全部是 `StrictModel`（`extra="forbid", strict=True`）。
- **怎么实现**：`apply_edit` 用 `model_validator` 强制"必须带 expected_sha256 或 old_text"；
  动作由 `agent/tools.py` 的 TypeAdapter 按 `tool` 字段判别解析。
- **为什么**：模型输出先过最严格 schema，多塞字段/类型不匹配都直接失败，从源头保证
  "模型永远只能走白名单动作、永远不能盲写"。

### 3.5 task/state.py —— 完成资格判定

- **角色**：把"状态是否健康 / 完成资格是否成立"收拢成纯函数。
- **做什么**：`RUNNING_STATUSES`（重启要转 interrupted 的集合）；`required_check_is_fresh`。
- **怎么实现**：`required_check_is_fresh` 要求 verified 且 `evidence_generation == edit_generation`。
- **为什么**：run_check 只对"它执行那一刻的文件版本"有效；只要后来又发生 apply_edit，
  旧证据代际就落后，必须判失效 —— 这是"任意成功命令不能替代原始验收"的落点。

### 3.6 api/tasks.py —— 流程第 1 / 6 阶段的 HTTP 层

- **角色**：把 TaskManager 暴露成 REST + SSE。
- **做什么**：建任务、查询、SSE 事件流、diff、Receipt 校验、命令审批、Apply、取消。
- **怎么实现**：SSE 按 `last-event-id`/`after` 游标增量拉事件；Receipt 校验同时返回
  "逻辑哈希"与"文件字节哈希"两项；写操作统一把 KeyError(404)/ValueError(400) 转 HTTP 错误。
- **为什么**：SSE 是单向事件流，断线重连靠游标续传；双哈希让 UI 能区分"Receipt 内容被改"
  与"Receipt 文件丢了/被换"两个不同失败。

### 3.7 task/service.py —— 流程第 1 / 6 阶段的生命周期

- **角色**：HTTP 层与 Runner 之间的胶水。
- **做什么**：建任务、持久化、恢复、命令审批、Apply、对外快照。
- **怎么实现**：`TaskRecord` 是内存态 + `asyncio.Event` 等待器；`TaskManager` 持 records
  字典；`_run` 作为后台 `asyncio.Task` 推进；每次变更 `_persist` 回 SQLite。
- **为什么**：**先落库再跑** —— 崩溃也不丢"建过任务"的事实；Runner 异常退出时把非终态
  如实标成 `FAILED_RECOVERABLE`，绝不伪装成 completed；审批用 Event 挂起/唤醒，避免轮询；
  Apply 前复核源仓库 HEAD/工作树/manifest 三样都没变，否则拒绝覆盖。

### 3.8 workspace/strategies.py —— 流程第 2 / 6 阶段的隔离与写回

- **角色**：保证模型碰不到真实仓库，写回前必先复核。
- **做什么**：`select_workspace` 决定用 detached worktree（干净 Git）还是 snapshot 副本；
  提供受控的 read/apply_edit/diff/apply。
- **怎么实现**：`_relative` 拒绝绝对路径与 `..`；`_is_protected` 拒绝 .env/锁文件/隐藏文件；
  `apply_edit` 校验前置 hash/old_text（old_text 做 CRLF 归一化、要求唯一）；`_atomic_writeback`
  逐文件临时写 + `os.replace` + fsync，失败用备份回滚。
- **为什么**：路径边界在解析层锁死，模型无法逃逸工作区；前置校验 + 原子写回让"编辑"
  既可审计又可回滚 —— 这是"系统敢把模型放进本地目录"的底气。

### 3.9 index/repo_index.py —— 流程第 2 阶段的侦察工具

- **角色**：给模型确定性、可复现的代码上下文。
- **做什么**：`RepoIndex.build` 扫仓库做 AST/行号索引（符号、导入边）；`source_context` 按
  goal 词频 + 显式 focus 路径选文件，拼成上下文。
- **为什么**：刻意不用 RAG —— 静态索引可审计、可复现、按行号可定位；评测上下文由
  失败输出聚焦生成，而不是黑盒检索。

### 3.10 llm/client.py —— 流程第 2 / 3 阶段的模型适配

- **角色**：把不同 provider 收敛成统一接口。
- **做什么**：`LLMClient` 提供 `plan` / `next_action` / `one_shot` 三个方法；
  `FakeLLM` 是注入式替身，供 smoke 与测试用。
- **怎么实现**：按 `resolved_transport` 选 AsyncAnthropic / AsyncOpenAI；每次请求前
  `ledger.reserve` 预留最坏情况额度，成功后 `ledger.commit` 换成实际用量。
- **为什么**：预算先预留后提交，避免两个评测 variant 互相透支；provider 报错只暴露
  "分类 + HTTP 状态码"，绝不留 API key 或响应原文。

### 3.11 agent/tools.py —— 流程第 3 阶段的第一道闸

- **角色**：模型输出进入系统前的最后一道格式闸。
- **做什么**：把模型 JSON 解析成唯一 `ToolAction`，失败抛 `InvalidToolActionError`。
- **怎么实现**：`TypeAdapter` + `Field(discriminator="tool")` 做可判别联合；`tool` 字段
  直接决定后续类型，无需手写 if/elif。
- **为什么**：解析失败是正常路径（模型常犯错）—— Runner 把它变成受限 observation 并计数，
  模型得不到任何额外能力，只会被迫回退到白名单工具。

### 3.12 task/runner.py —— 流程第 3 阶段的核心循环 ★

- **角色**：把"模型→动作→观察"闭环跑起来，并判定是否够格生成 Receipt。
- **做什么**：建工作区 → 索引 → 计划 → 循环 `next_action` → `parse` → `_dispatch` → observation；
  最后在满足完成资格时 `_create_receipt`。
- **怎么实现**：循环里 `apply_edit` 成功即 `edit_generation += 1` 并清空 `required_check_verified`；
  `run_check` 成功且 argv 与 required 一致 → 写 `evidence_generation`；`finish(verified)` 只有
  `required_check_is_fresh` 成立才走 Receipt。
- **为什么**：一切进入观察列表供模型决策，但模型只能看白名单动作结果；"编辑代际"
  是安全核心 —— 从机制上排除"先蒙对一个检查、再乱改代码"。

### 3.13 policy/commands.py —— 流程第 4 阶段的门禁与执行

- **角色**：决定命令能不能直接跑，并负责安全执行。
- **做什么**：`classify_argv` 三档分类；`ProcessExecutor.run` 用 `shell=False` 执行。
- **怎么实现**：先查 `_safe_readonly_check`（pytest/unittest/compileall/git status 等），
  再查 `HIGH_RISK`/`NETWORK_RISK` 正则，剩下的默认进审批；执行用
  `asyncio.create_subprocess_exec`，支持超时/取消/输出截断；`python -m pytest` 会解析到
  当前解释器但对外 argv 保持原样。
- **为什么**：默认保守 —— 没有被明确认定为只读的命令都要人来拍板；argv + `shell=False`
  杜绝 shell 拼接；本地执行器不是容器，缺 Docker 时公开评测会被阻断而非冒充沙箱。

### 3.14 receipt/sealer.py —— 流程第 5 阶段的完成证据

- **角色**：把任务所有关键事实密封成一份不可抵赖的 Receipt。
- **做什么**：`seal_receipt` 算自校验哈希；`write_receipt_atomic` 原子落盘；`verify_receipt` /
  `verify_receipt_file` 分别校验逻辑与文件字节。
- **怎么实现**：计算前先剔除自指字段（否则 receipt_hash 参与自身输入变成不动点）；
  文件字节哈希单独存 SQLite。
- **为什么**：能区分"Receipt 内容被改"与"Receipt 文件被换/丢了"两个不同失败；
  原子写 + fsync 保证写一半崩了不会留下半截文件。

### 3.15 evidence/canonical.py —— 贯穿全程的哈希基石

- **角色**：所有证据哈希的唯一入口。
- **做什么**：`canonical_json`（固定 sort_keys + 无空格 + ensure_ascii=False）+ 哈希包装。
- **为什么**：事件链、Receipt、报告 ID 都以哈希互指，序列化不稳定则哈希不可复现；
  这里就是那个"唯一真相格式"。

---

## 4. 其他模块简介

| 模块 | 在程序中干什么（一句话） |
|---|---|
| `api/common.py` | 路由共享：项目路径守卫、评测请求 schema |
| `api/evaluation.py` | 评测路由：health / suites / preflight / runs / reports（报告查 SQLite 非内存） |
| `llm/budget.py` | 共享预算账本：每次请求前按最坏情况预留，防止两个变体互相透支 |
| `evals/benchmark.py` | 评测 CLI + BenchmarkHarness：确定性 smoke 与受限 real 对比 |
| `evals/orchestrator.py` | 成对评测编排：初始失败门禁、共享预算、追加式 JSONL 报告 |
| `evals/models.py` | 版本化 `BenchmarkCase` v2：case 的完整不可变契约 + 校验器 |
| `evals/utils.py` | 纯函数助手：manifest 加载、指标聚合、成本估算、原子写 JSON |
| `docker/executor.py` | 注入式 Docker 执行：preflight、`--read-only --network none` 硬隔离 |
| `docker/evaluator_image.py` | 镜像构建与不可变 lock 产物（校验和 + 运行时探测） |
| `corpus/loader.py` | 语料加载 + 内容寻址拉取计划（规划与执行分离，先确认再下载） |
| `corpus/public_resolver.py` | BugsInPy 官方元数据溯源：解析到不可变 commit + 运行时失败探针 |
| `faults/scenarios.py` | 离线故障注入：12 个场景验证安全不变量真的成立 |
| 顶层 `*.py`（薄壳） | 仅为历史导入路径的 re-export 兼容层，无业务逻辑 |

---

## 5. 串联成整个程序

### 5.1 一条主线：从输入到输出

把第 2 节七阶段串起来，整个程序其实就是一条**"证据流水线"**：

> **输入** = 目标仓库 + 一段问题描述（goal）+ 一条验收命令（check_command）
>
> **处理** = 建任务并落库 → 隔离副本 → 让模型在副本上做计划 + 走 typed 工具循环 →
> 用策略门禁跑验收 → 把每一步动作/观察写进 SHA-256 事件链 → 只在"最新编辑后原命令成功"
> 时密封 Patch Receipt → 停在 awaiting_apply
>
> **输出** = 隔离副本里的修改（diff）+ 可回放的事件链 + 可自校验的 Patch Receipt
>
> **终局** = 人审 diff 后按 Apply → 复核源仓库未变 → 原子写回 → completed

每一段处理都对应一个明确的模块职责：**管配置的是 config，管 HTTP 的是 api，管生命周期
的是 task/service，管执行动作的是 task/runner + agent/tools，管隔离的是 workspace，
管命令安全的是 policy，管落库与证据的是 infrastructure/sqlite + evidence/canonical，
管完成判定的是 task/state，管最终证据的是 receipt/sealer。**

### 5.2 关键数据流（模块间流动的是"什么"）

- **任务状态**：`api/tasks` → `task/service`（内存 `TaskRecord`）→ `task/runner`（推进）→
  `infrastructure/sqlite`（持久化）。状态机取值在 `task/models.py` 定义。
- **模型输出**：`llm/client` → `agent/tools`（校验成 typed action）→ `task/runner::_dispatch`
  （分发执行）。
- **命令**：`task/runner::_run_check` → `policy/commands`（分类/审批）→ 执行器（进程或 Docker）
  → 结果回 `task/runner`，只有"原 argv 成功"才更新完成资格。
- **证据**：`task/runner` 的每次动作 → `infrastructure/sqlite::append_event`（链式哈希）→
  `receipt/sealer`（最终密封，文件哈希回存 SQLite）。

### 5.3 关键安全属性由哪些模块共同保证

| 安全属性 | 由谁保证 |
|---|---|
| 模型只能走 6 个白名单工具、不能盲写 | `agent/tools.py` + `task/models.py`（extra=forbid、apply_edit 强制前置条件） |
| 任意成功命令不能替代原始验收 | `task/state.py` + `task/runner.py`（argv 精确比较 + 编辑代际） |
| 编辑让旧验证失效 | `task/runner.py`（edit_generation 递增） |
| 模型碰不到真实仓库 | `workspace/strategies.py`（worktree/snapshot + 路径边界） |
| 过程可回放、篡改可检测 | `infrastructure/sqlite.py`（事件链）+ `evidence/canonical.py`（稳定哈希） |
| 完成证据不可抵赖 | `receipt/sealer.py`（逻辑哈希 + 文件字节哈希） |
| 风险命令要人批、写回要人裁 | `policy/commands.py` + `task/service.py`（审批 Event + Apply 复核） |

### 5.4 最后一段话

如果你只记住一个模型：**PatchProof 是一条"证据流水线"**。它不信任模型说的"我改好了"，
而是把模型关进隔离副本、逼它只走白名单工具、把每一步写进哈希链、强制"最后一次编辑后按
原始命令真实跑通"，最后把这一切密封成可自校验的 Receipt，交给人类裁决。每一环的代码
都可以在这份指南的第 2 节（流程）、第 3 节（模块）、第 5.2/5.3 节（数据流与安全属性）里
找到对应的落点。
