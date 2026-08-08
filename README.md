<p align="center">
  <img src="docs/hero.svg" alt="PatchProof" width="100%">
</p>

<div align="center">

# PatchProof

### 让 AI 编码 Agent 证明自己「真的改完了」，而不是嘴上说改完了

![version](https://img.shields.io/badge/version-v0.3.7-8b5cf6)
![python](https://img.shields.io/badge/python-3.8%2B-blue)
![license](https://img.shields.io/badge/license-MIT-34d399)
![BugsInPy](https://img.shields.io/badge/BugsInPy-3%20official%20cases-22d3ee)
![complete pairs](https://img.shields.io/badge/complete%20pairs-3-34d399)
![auto apply](https://img.shields.io/badge/auto%20apply-0-fb7185)

</div>

---

## 目录

- [它是什么](#它是什么)
- [一句话看懂](#一句话看懂)
- [通俗版：五道关卡](#通俗版五道关卡)
- [实测结果：BugsInPy 官方 case](#实测结果bugsinpy-官方-case)
- [实测结果：自持 mini 语料](#实测结果自持-mini-语料)
- [为什么证据可信](#为什么证据可信)
- [对结果的诚实边界](#对结果的诚实边界)
- [真实基建 bug 复盘](#真实基建-bug-复盘)
- [快速开始](#快速开始)
- [Benchmark 用法](#benchmark-用法)
- [架构](#架构)
- [安全模型](#安全模型)
- [已知局限](#已知局限)
- [文档](#文档)

---

## 它是什么

PatchProof 不是又一个「让 AI 帮你改代码」的工具，而是专门回答所有 AI 编程工具都绕不开的问题——**模型说它改完了，你凭什么信？**

它把「完成」从模型的自述，变成可审计、可复核、可追责的**证据链**：

- 模型只能在一个**隔离副本**里、用 **6 个受控工具**干活；
- 每一步动作与观察都写进一条 **SHA-256 哈希链**；
- 说「完成了」之前，必须用**你指定的那一条命令**、在改动之后、真实跑通；
- 最后生成一张 **Patch Receipt**，你看 diff、你决定要不要 Apply。

> **一句话：PatchProof 把一个「AI 说改完了」的黑盒，变成「你随时能查账、能验收、能追责」的透明过程。**

---

## 一句话看懂

```
你给的任务（目标 + 损坏仓库）
   │
   ▼
模型只能用 6 个受控工具干活 ──► 每走一步，动作/结果都写进
(搜代码 / 读文件 / 改代码 /           不可篡改的 SHA-256 事件链
 看diff / 跑测试 / 声明完成)
   │
   ▼
必须用你指定的那条验证命令、在改完代码之后、真实跑通 ──► 才算「完成」
   │
   ▼
生成一张 Patch Receipt（改了什么、前后哈希、测试结果、谁批准的）
   │
   ▼
你看 diff → 你决定要不要 Apply
```

---

## 通俗版：五道关卡

把 AI 想成装修工。你请他来修水管，PatchProof 是全程跟着他的监理，保证三件事：**他没乱来、他真的修好了、你事后能查证。**

| 关卡 | 内容 | 说明 |
|---|---|---|
| 🧱 第一关 · 隔离 | AI 只在隔离副本里工作 | 怎么改都碰不到你真代码，改完出一份 diff，你点头它才敢动原文件 |
| 🔐 第二关 · 留痕 | 只准用 6 种工具 | 搜代码/读文件/改代码/看diff/跑测试/声明完成；不能随意执行 shell、联网、删文件 |
| ✍️ 第三关 · 有凭有据 | 改代码必须给精确旧片段 | `apply_edit` 需要字节级匹配的 `old_text` 或文件哈希；编一段不存在的代码？直接拒绝 |
| ✅ 第四关 · 当场验收 | 说完成前必须跑通 | 用你指定的那一条命令，在最近一次改动之后真实跑通；跑 `python --version` 糊弄你，不算数 |
| 📋 第五关 · 验收单 | 一张不可伪造的 Patch Receipt | 文件前后哈希、diff 哈希、测试结果、审批轨迹，基于整条事件链盖哈希戳，原子写入 |

**预算**：每次模型调用前先按最坏情况冻结额度；请求数、token、花费进共享账本并有硬上限，烧完立刻停。

**诚实**：环境装不起来、钱不够、输出被截断、编辑前置校验没过——每种情况都有明确分类，**不会把一个跑了一半的结果包装成「成功」**。

---

## 实测结果：BugsInPy 官方 case

3 个 BugsInPy 官方 case 通过全链路评测（baseline one-shot 对照 + harness 工具循环），**全部为 complete pair（双通过）**，全部在 Docker 隔离镜像、共享预算、永不自动 Apply 下完成。

### 一览

| case | 真实 bug | baseline one-shot | harness tool-loop | pair |
|---|---|---|---|---|
| 🟢 `pysnooper-1` | 中文源码被 `ascii` 解码乱码 | ✅ success | ✅ success · 7 步 | **complete** |
| 🟢 `pysnooper-3` | 文件输出引用未定义 `output_path` | ✅ success | ✅ success · 3 步 | **complete** |
| 🟢 `fastapi-1` | `jsonable_encoder` 缺 `exclude_defaults` 参数 | ✅ success | ✅ success · 7 步 | **complete** |

- **baseline one-shot 3 / 3 通过；harness tool-loop 3 / 3 通过；complete pair 3 / 3**
- 全部 3 个 case 都通过「初始失败门禁」：两个隔离副本产生**一致的、真实的非零失败**，失败原因是 bug 本身，不是环境问题
- 失败会被诚实分类并记录，绝不重跑、绝不静默重试美化结果

> 另：BugsInPy 官方语料中 `pysnooper-2`（`custom_repr` 跨 3 处接线）已接入评测但模型在共享预算内未能完成修复，按预算耗尽如实分类、不参与 3 / 3 计分——不刷榜，失败就是失败。

### 逐个明细

#### 🟢 `bugsinpy-pysnooper-1` — 中文源码解码乱码（PySnooper issue #124）

官方修复：`get_source_from_frame` 无 PEP-263 声明时按 Python 3 默认的 `utf-8` 解码，替代 `ascii`。

| 指标 | baseline one-shot | harness tool-loop |
|---|---|---|
| 结果 | ✅ success | ✅ success（`awaiting_apply`） |
| 修改文件 | `pysnooper/tracer.py` | `pysnooper/tracer.py` |
| patch 大小 | 443 B | 443 B |
| steps / requests | 1 / 1 | 7 / 9 |
| required-check / receipt / 事件链 | n/a（one-shot） | ✅ / ✅ / ✅ |

#### 🟢 `bugsinpy-pysnooper-3` — 文件输出引用未定义变量（PySnooper Fix #2）

官方修复：`write()` 闭包把未定义的 `output_path` 改为 `output`。buggy 版本在 `@pysnooper.snoop('/path/log')` 写日志时抛 `NameError: name 'output_path' is not defined`。

| 指标 | baseline one-shot | harness tool-loop |
|---|---|---|
| 结果 | ✅ success | ✅ success（`awaiting_apply`） |
| 修改文件 | `pysnooper/pysnooper.py` | `pysnooper/pysnooper.py` |
| patch 大小 | 399 B | 399 B |
| steps / requests | 1 / 1 | 3 / 4 |
| required-check / receipt / 事件链 | n/a | ✅ / ✅ / ✅ |

#### 🟢 `bugsinpy-fastapi-1` — `jsonable_encoder` 缺参数（FastAPI PR #1166）

官方修复：为 `jsonable_encoder` 增加 `exclude_defaults` / `exclude_none` 参数。测试 `jsonable_encoder(model, exclude_defaults=True)` 在 buggy 版本上抛 `TypeError`。

| 指标 | baseline one-shot | harness tool-loop |
|---|---|---|
| 结果 | ✅ success | ✅ success（`awaiting_apply`） |
| 修改文件 | `fastapi/encoders.py` | `fastapi/encoders.py` |
| patch 大小 | 2165 B | 874 B |
| steps / requests | 1 / 1 | 7 / 9 |
| required-check / receipt / 事件链 | n/a | ✅ / ✅ / ✅ |

> 这个 case 一开始 one-shot 连续失败，查出的原因正是框架自己的一个真实缺陷（见下方 [基建 bug 复盘](#真实基建-bug-复盘) 第 6 条）：失败输出只点名函数 `jsonable_encoder`、没点名文件路径，导致聚焦上下文把 `fastapi/encoders.py` 挤出了窗口，模型无精确文本可抄。修复后 one-shot 稳定通过，与 harness 组成 complete pair。

### 评测配置

| 项 | 值 |
|---|---|
| 模型 | `deepseek-v4-flash`（OpenCode `go` 网关，OpenAI-compatible transport） |
| 推理 | 关闭（`PATCHPROOF_LLM_REASONING=off`，避免输出预算被 reasoning 吃掉） |
| 隔离 | Docker 钉死 digest（Python 3.8.20，`--network none`，只读 rootfs，无 docker socket） |
| 失败门禁 | 两个隔离副本必须产生**一致的非零失败**；环境不可复现/超时/不一致不参与评分 |
| 预算 | 共享 requests / tokens / cost 账本，硬上限，超出即停 |
| Apply | 永不自动 Apply；所有写回前人工确认 |

> **诚实声明（provenance）**：PySnooper / FastAPI 的官方测试文件依赖 `python_toolbox` 等评测镜像未安装的库，本仓库使用**自包含的重建版本**（按官方测试断言逐字节验证：buggy 失败 / 官方修复后通过），与官方测试同语义，随仓库维护在 `benchmarks/public/<project>-<bug>/`。评测语义 = 官方测试作为契约提供给模型（SWE-bench 范式），**库代码的修复全部由模型完成**。评测镜像额外安装 `fastapi==0.53.2` / `pydantic==1.8.2` / `starlette==0.13.2` / `future` / `decorator` / `six`（钉死版本，见 `docker/evaluator/requirements.lock`）。

> **复现公开 case**：`data/eval-cache/` 已被 `.gitignore` 忽略（第三方源码不下沉仓库）。clone 后需先 `build-evaluator-image` + `resolve-public`，再把 `benchmarks/public/<project>-<bug>/` 下的重建测试契约复制进对应快照，然后跑 `real` 评测。具体命令见 [docs/EVALUATION.md](docs/EVALUATION.md)。

---

## 实测结果：自持 mini 语料

PatchProof 自持的 5 个 mini fixture（单文件、带意图说明）——作用是**验证管道**，5 / 5 通过，harness 侧全部 `awaiting_apply`，`required_check_verified` / `receipt_verified` / `receipt_file_verified` / `event_chain_verified` 全为真，`precondition_failures == 0`。总成本约 **$0.14**。

| case | 注入的 bug | baseline one-shot | harness tool-loop |
|---|---|---|---|
| mini-validation | 邮箱校验缺规范化 | ✅ | ✅ |
| mini-config-precedence | 超时配置优先级反了 | ✅ | ✅ |
| mini-pagination | 边界 `offset+limit==len` 判错 | ✅ | ✅ |
| mini-idempotency | 重复请求未幂等 | ✅ | ✅ |
| mini-serialization | v1→v2 解码不兼容 | ✅ | ✅ |

---

## 为什么证据可信

- **required-check 资格门禁**（`runner.py`）：只有与任务 `check_command` **完全一致**的 argv，在**最近一次编辑之后**成功执行，`finish(verified)` 才被接受。
- **不可篡改事件链**（`storage.py`）：计划、动作、观察、审批、结果全部串 SHA-256 哈希链，篡改即暴露。
- **Patch Receipt**（`receipt.py`）：目标、计划、工具统计、文件前后哈希、diff 哈希、命令退出码、审批轨迹、测试结果统一为可验证 JSON，原子写入 `data/runs/<id>/receipt.json`。
- **编辑前置校验**：`apply_edit` 必须提供精确唯一 `old_text` 或匹配的 `expected_sha256`，禁止盲写；行尾无关匹配（CRLF/LF 兼容）。
- **诚实失败分类**：`baseline_precondition_failed` / `llm_budget_exhausted` / `provider_output_truncated` / `environment_unreproducible`…失败就是失败，绝不把部分结果包装成成功。
- **永不自动 Apply**：receipt 生成后必须人工 review diff 再 Apply；Apply 前校验 HEAD、工作树与源文件 manifest。

---

## 对结果的诚实边界

- **官方案例覆盖真实项目**：PySnooper（2 个真实 issue）与 FastAPI（1 个真实 PR）的官方 bug 与官方修复 commit 均可溯源；但模型能力结论仅覆盖「Python 3.8、单/双文件、有测试契约」的任务。
- **失败如实呈现**：`pysnooper-2`（跨 3 处接线的 `custom_repr`）已接入评测，模型在共享预算内未完成，按预算耗尽如实分类、不参与计分，也不做重跑美化。
- **mini 语料**是 PatchProof 自持 fixture，含意图注释，适合冒烟、不适合当难度基准。
- 尚未与 OpenHands / Aider / SWE-agent 做同 case 对照实验；更大规模的真实修复能力需要更大语料才能下结论。

---

## 真实基建 bug 复盘

在真实评测跑通过程中，暴露了 5 个单测覆盖不到的基础设施问题（每个都可审计、可回滚）：

| # | Bug | 影响 | 修复 |
|---|---|---|---|
| 1 | Docker `--mount ...,rw` 裸布尔字段被 29.6.2 拒绝 | 容器无法启动，所有 Docker 评测/探针全挂 | `docker_executor.py` |
| 2 | 默认开启推理吃掉全部输出预算 | 任何请求 `provider_output_truncated` | `config.py` + `llm.py` 加 `PATCHPROOF_LLM_REASONING` |
| 3 | `_iter_files` 用绝对路径 parts 排除目录 | 位于 `data/` 下的仓库索引为空 | `repo_index.py` |
| 4 | **CRLF/LF 不匹配**：Windows 上 git `core.autocrlf=true` 检出，模型发出的 `old_text`（`\n`）匹配不到 `\r\n` 文件 | 所有 `apply_edit` 前置失败 | `workspace.py` 行尾无关匹配 |
| 5 | **时间戳不定性**：测试输出带 `HH:MM:SS`，两次门禁运行不一致 | `initial_check_nondeterministic` 拦死真实评测 | `artifact_policy.py` 归一化 ISO 时间戳 |
| 6 | **one-shot 聚焦上下文漏文件**：失败输出只点名函数名（`jsonable_encoder() got an unexpected keyword...`）不点名文件路径，确定性索引把缺陷文件 `fastapi/encoders.py` 挤出 top-6 | fastapi-1 的 baseline one-shot 连续 3 次 `baseline_precondition_failed`（模型无精确文本可抄） | `evaluation.py` 按整词匹配失败输出中的符号名，把定义该符号的文件加入聚焦；`repo_index.py` 保证聚焦文件优先入选 |

---

## 快速开始

后端：

```powershell
.venv\Scripts\python.exe -m uvicorn patchproof.api:app --app-dir src --port 8010
```

> **Windows 注意**：不要加 `--reload`。`uvicorn --reload` 在 Windows 上会把 worker 跑在
> `SelectorEventLoop` 上，导致 `run_check`（`asyncio.create_subprocess_exec`）抛
> `NotImplementedError`，所有任务都无法通过验证。`src/patchproof/api.py` 已显式固定
> Windows Proactor 事件循环策略，配合不加 `--reload` 即可正常运行。

前端：

```powershell
cd frontend
pnpm install
pnpm run dev
```

打开 `http://localhost:5175`。默认目标仓库为 PatchProof 自带的 `benchmarks/fixtures/validation` 安全 fixture。

---

## 一键自托管部署

Linux 服务器已安装 Docker Engine + Compose v2，且域名已解析到服务器时，在仓库
根目录运行一行命令：

```bash
bash deploy/deploy.sh patchproof.example.com
```

Caddy 会自动配置 HTTPS；域名同时驱动后端 CORS，无需手改配置。暂时没有域名可用
`bash deploy/deploy.sh --localhost` 启动仅限本机的 HTTP 冒烟模式。常用运维命令为
`bash deploy/deploy.sh status`、`upgrade`、`logs` 和 `uninstall`，完整说明见
[部署文档](docs/DEPLOYMENT.md)。

持久数据位于 `deploy/data/`，放入 `deploy/repositories/<name>` 的任务仓库在应用内
使用路径 `/repositories/<name>`。脚本不处理 LLM key，用户继续在浏览器中自带 key。

> 评测边界：默认 Compose 不挂载宿主机 Docker socket。Docker 只承载本应用，
> 不等于 Web 后端具备 Docker 评测能力；`benchmark real` / 公开评测会诚实地保持
> blocked，而不会退回本地执行器冒充隔离评测。

---

## Benchmark 用法

确定性 smoke suite（不调用模型，验证管道）：

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark smoke `
  --manifest benchmarks/manifest.v2.json --project-root . --output data/benchmark-smoke.json
```

安全钩子故障注入：

```powershell
.venv\Scripts\python.exe -m patchproof.faults run --output data/fault-report.json
```

真实模型评测（显式确认 + 预算上限，永不自动 Apply）：

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark real `
  --manifest data/bugs-in-py.resolved.v3.run.lock.json `
  --project-root . --output data/benchmark-real.json `
  --confirm-real --confirm-public-code-egress `
  --max-cases 4 --repeats 1 --max-requests 100 --max-tokens 600000 --max-cost-usd 4
```

Provider 配置只读自 `archive/researchflow/.env`（DEEPSEEK_* / ANTHROPIC_*），密钥不复制、不落盘、不进日志。

---

## 架构

| 模块 | 职责 |
|---|---|
| `runner.py` | typed tool loop、required-check 资格门禁、receipt 生成 |
| `workspace.py` | detached worktree / snapshot fallback、编辑前置校验、写回校验 |
| `storage.py` | SQLite 可恢复状态机、SHA-256 事件链 |
| `llm.py` | Anthropic / OpenAI-compatible 双 transport、预算账本 |
| `policy.py` | argv + `shell=False` 策略，风险命令人工审批 |
| `benchmark.py` / `evaluation.py` | smoke / resolve-public / real 评测，诚实失败分类 |
| `docker_executor.py` | 钉死 digest 的隔离执行器 |
| `public_resolver.py` | BugsInPy 解析、HEAD/镜像校验、可执行状态标记 |
| `evaluator_image.py` | 镜像构建、runtime 探针、不可变 lock 产物 |
| `receipt.py` / `artifact_policy.py` | receipt 密封与篡改检测、deny 策略 |
| `frontend/` | 轻量 UI（任务、事件链、receipt、diff review） |

---

## 安全模型

- 执行器 `argv + shell=False`；组合命令、联网、安装、删除、Git 写操作需人工审批。
- 本地 process executor 明确标记 `local_smoke_only`，绝不冒充容器隔离。
- `apply_edit` 需要 `expected_sha256` 或精确 `old_text` 前置条件；删除不静默写回；敏感/隐藏文件默认禁编辑。
- `bug_patch.txt` 等已知答案 artifact 永不索引、不进入工作区、不暴露给 prompt/报告。

---

## 已知局限

- 官方案例仅覆盖 Python 3.8 生态（BugsInPy 中 3.6/3.7 项目与评测镜像不兼容，诚实标记 `environment_unreproducible` 并排除）。
- 模型能力结论样本仍小（3 个官方 case）；`pysnooper-2` 是如实记录、不计分的失败样本。
- mini fixture 含意图注释，适合冒烟不适合当难度基准。
- 尚未与 OpenHands / Aider / SWE-agent 做同 case 对照实验。
- 无 LSP/语义导航，代码上下文为确定性静态索引。

---

## 待实现（Roadmap）

按优先级排序：

| 状态 | 功能 | 说明 |
|---|---|---|
| ✅ 已实现 | **一键自托管部署** | 单命令 Docker Compose + Caddy；域名自动 HTTPS，支持 localhost smoke、健康检查和幂等运维 |
| ✅ 已实现 | **自带 API key（per-user provider）** | 前端填 base_url / model / api_key，随任务走；后端按请求构建独立 `LLMClient`，key 只存内存、不落库不进日志，`deploy/` 构件已随仓库提供 |
| 📌 规划中 | **远端 git 仓库支持（任务路径）** | `repo_path` 接受 `https://github.com/...` 时自动 clone 到工作区再快照；只允许 HTTPS，与评测路径 `confirm_download` 门禁对齐（方式 B） |
| 📌 规划中 | **简单用户体系 / 鉴权** | 反向代理层 Basic Auth 起步，后续可加登录；配合自带 key 支持多用户共享部署 |
| 💭 备选 | **更多 BugsInPy 官方 case** | 扩展语料分布（不同难度/类别） |
| 💭 备选 | **LSP / 语义导航** | 给工具循环加基于符号的精准跳转，替代纯静态索引 |

部署方案见 [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)。

---

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
- [docs/BENCHMARK.md](docs/BENCHMARK.md)
- [docs/DATASET.md](docs/DATASET.md)
- [docs/DOCKER.md](docs/DOCKER.md)
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)
- [docs/EVALUATION.md](docs/EVALUATION.md)
