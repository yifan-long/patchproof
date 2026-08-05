# PatchProof

> 让 AI 编码 Agent 证明自己"真的改完了"，而不是嘴上说改完了。

PatchProof 是一个**约束并验证编码 Agent** 的框架：它不是又一个"让 AI 帮你改代码"的工具，而是专门回答所有 AI 编程工具都绕不开的问题——**模型说它改完了，你凭什么信？**

## 目录

- [它是什么](#它是什么)
- [一句话看懂](#一句话看懂)
- [通俗版：它到底怎么工作](#通俗版它到底怎么工作)
- [真实例子：BugsInPy 官方 bug](#真实例子bugsinpy-官方-bug)
- [它解决什么、不解决什么](#它解决什么不解决什么)
- [为什么证据可信](#为什么证据可信)
- [实测评测结果](#实测评测结果)
- [真实基建 bug 复盘](#真实基建-bug-复盘)
- [快速开始](#快速开始)
- [Benchmark 用法](#benchmark-用法)
- [架构](#架构)
- [安全模型](#安全模型)
- [已知局限](#已知局限)
- [文档](#文档)

---

## 它是什么

你丢给它一个"故意坏了"的代码仓库，它在隔离环境里驱动模型完成闭环，并把每一步变成可验证的证据。核心只有一句：**"完成"必须靠证据证明，不能靠模型自述。**

---

## 一句话看懂

```
你给的任务
   │
   ▼
模型只能用 6 个受控工具干活 ──► 每走一步，动作/结果都写进
(搜代码 / 读文件 / 改代码 /           不可篡改的 SHA-256 事件链
 看diff / 跑测试 / 声明完成)
   │
   ▼
必须用你指定的那条验证命令、在改完代码之后、真实跑通 ──► 才算"完成"
   │
   ▼
生成一张 Patch Receipt（改了什么、前后哈希、测试结果、谁批准的）
   │
   ▼
你看 diff → 你决定要不要 Apply
```

---

## 通俗版：它到底怎么工作

把 AI 想成装修工。你请他来修水管（改代码），PatchProof 是全程跟着他的监理，保证三件事：**他没乱来、他真的修好了、你事后能查证。**

### 第一关：隔离——不让 AI 直接动你的东西

AI 工作的不是你的仓库，而是一个**隔离副本**（快照或 git worktree）。它怎么改都碰不到你真正的代码，改完出一份 diff，你点头它才敢碰原文件。就像装修工先在样板间练手，不直接拆你家的墙。

### 第二关：只准用 6 种工具，干的每件事都留痕

模型每走一步，只能从 6 个动作里选一个：**搜代码、读文件、改代码、看 diff、跑测试、声明完成**。它不能凭空执行 shell、不能联网、不能删文件（这些都要人工审批）。

它的每一次动作、每一条观察，都被记进一条 **SHA-256 哈希链**——就像施工日志，每天现场拍照签字；想篡改任何一天，整本日志的校验就对不上。

### 第三关：改代码必须"有凭有据"

模型要改代码，必须给出当前文件里**精确存在的旧片段**（或匹配的文件哈希），PatchProof 校验通过才写入。它编一段文件里根本不存在的代码？直接拒绝。这就堵死了"AI 幻觉代码"最常见的坑。

### 第四关：说"完成了"之前，必须当场验收

模型想声明完成？必须用**你指定的那一条验证命令**（比如 `pytest`），在它最后一次改动之后，真实跑通。而且必须是同一条命令——它跑个 `python --version` 这种跟任务无关的命令糊弄你，不算数。

### 第五关：一张不可伪造的验收单

全部通过后，PatchProof 生成一张 **Patch Receipt**：改了什么文件、每个文件改动前后的哈希、diff 的哈希、跑的命令和退出码、谁批准了什么、测试结果，最后盖一个基于整条事件链的哈希戳。

这张单子是原子的（要么完整写入，要么不写）。你拿它去复核 diff，满意了才 **Apply** 回真实仓库。

### 预算：AI 不能无限烧钱

每次模型调用前先按最坏情况"冻结"预算额度；请求数、token 数、花费都进共享账本并有硬上限。钱烧完了立刻停，不会悄悄跑飞。

### 诚实：失败就是失败

环境装不起来、模型钱不够、输出被截断、编辑前置校验没过——每种情况都有明确分类，**不会把一个跑了一半的结果包装成"成功"**。

> **一句话：PatchProof 把一个"AI 说改完了"的黑盒，变成"你随时能查账、能验收、能追责"的透明过程。**

---

## 真实例子：BugsInPy 官方 bug

让 `deepseek-v4-flash` 修 PySnooper issue #124（中文源码被 `ascii` 解码乱码）：

**模型在 8 步内完成：** 读失败测试 → 定位 `pysnooper/tracer.py` → 把 `encoding = 'ascii'` 改成 `'utf-8'` → 跑官方测试 → 通过 → 生成 receipt → 等待人工 Apply。

```
✅ 初始失败证据：pytest -q tests/test_chinese.py::test_chinese  →  returncode 1（中文乱码断言失败）
✅ required-check： 最后一次编辑后，用完全一致的命令真实跑通
✅ Patch Receipt：  文件前后哈希、diff 哈希、测试结果、事件链头，全部验证通过
✅ 提交给人类 review，不自动写回仓库
```

完整实测结果见下方 [实测评测结果](#实测评测结果)。

---

## 它解决什么、不解决什么

**解决**：AI 改代码的结果**不可验证**、**不可追责**。PatchProof 把"完成"从模型的自述变成可审计的证据。

**不解决（也明确不假装解决）**：
- 不是又一个 IDE 插件 / 终端 Agent，不追求"工具多、改得快"。
- 不给模型任意 shell 权限，组合命令、联网、删除都要人工审批。
- 不自动把你的仓库改掉——永远先出 diff，你确认后才 Apply。
- 本地进程执行器诚实标注 `local_smoke_only`，绝不冒充 Docker 隔离。

---

## 为什么证据可信

- **required-check 资格门禁**（`runner.py`）：只有与任务 `check_command` **完全一致**的 argv，在**最近一次编辑之后**成功执行，`finish(verified)` 才被接受。
- **不可篡改事件链**（`storage.py`）：计划、动作、观察、审批、结果全部串 SHA-256 哈希链，篡改即暴露。
- **Patch Receipt**（`receipt.py`）：目标、计划、工具统计、文件前后哈希、diff 哈希、命令退出码、审批轨迹、测试结果、verdict 统一为可验证 JSON，原子写入 `data/runs/<id>/receipt.json`。
- **编辑前置校验**：`apply_edit` 必须提供精确唯一 `old_text` 或匹配的 `expected_sha256`，禁止盲写；行尾无关匹配（CRLF/LF 兼容）。
- **诚实失败分类**：`baseline_precondition_failed` / `llm_budget_exhausted` / `provider_output_truncated` / `environment_unreproducible`…失败就是失败，绝不把部分结果包装成成功。
- **永不自动 Apply**：receipt 生成后必须人工 review diff 再 Apply；Apply 前校验 HEAD、工作树与源文件 manifest。

---

## 实测评测结果

### 配置

| 项 | 值 |
|---|---|
| 模型 | `deepseek-v4-flash`（OpenCode `go` 网关，OpenAI-compatible transport） |
| 推理 | 关闭（`PATCHPROOF_LLM_REASONING=off`，避免输出预算被 reasoning 吃掉） |
| 隔离 | Docker 钉死 digest，`--network none`，只读 rootfs，无 docker socket |
| 失败门禁 | 两个隔离副本必须产生**一致的非零失败**，环境不可复现/超时/不一致不参与评分 |
| 预算 | 共享 requests/tokens/cost 账本，硬上限，超出即停 |
| Apply | 永不自动 Apply；所有写回前人工确认 |

### 结果

#### PatchProof 自持 mini 语料 —— 5 / 5 通过

| case | baseline one-shot | harness tool-loop |
|---|---|---|
| mini-validation | ✅ | ✅ |
| mini-config-precedence | ✅ | ✅ |
| mini-pagination | ✅ | ✅ |
| mini-idempotency | ✅ | ✅ |
| mini-serialization | ✅ | ✅ |

harness 侧全部 `awaiting_apply`，`required_check_verified` / `receipt_verified` / `receipt_file_verified` / `event_chain_verified` 全为真，`precondition_failures == 0`。总成本约 **$0.14**。

#### BugsInPy 官方公开 case —— 1 / 1 通过，complete pair

`bugsinpy-pysnooper-1`（PySnooper issue #124，官方修复 commit `56f22f8`）：

| 指标 | baseline one-shot | harness tool-loop |
|---|---|---|
| 结果 | ✅ success | ✅ success（`awaiting_apply`） |
| steps / tool calls | 1 / 0 | 8 / 8 |
| 修改文件 | `pysnooper/tracer.py` | `pysnooper/tracer.py` |
| patch 大小 | 443 B | 443 B |
| 失败分类 | — | —（0 前置失败） |
| required-check / receipt / 事件链 | n/a（one-shot） | ✅ / ✅ / ✅ |

- `complete_pairs: 1, partial_runs: 0` —— baseline 与 harness 的完整对照成立
- 门禁初始证据：`pytest -q -s tests/test_chinese.py::test_chinese` returncode 1（真实断言失败：中文源码行被 `ascii` 解码乱码）
- 修复内容：`get_source_from_frame` 无 PEP-263 声明时默认 `ascii` 解码 → 改为 Python 3 默认的 `utf-8`
- 总成本约 **$0.10**（10 次请求）

> **诚实声明（provenance）**：PySnooper 官方 `tests/test_chinese.py` 依赖 `python_toolbox`，评测镜像（Python 3.8）未安装；本仓库使用**自包含的重建版本**（按官方测试断言逐字节验证过：buggy 失败 / 一行修复后通过），与官方测试同语义，MIT 许可，随仓库维护在 [`benchmarks/public/pysnooper-1/test_chinese.py`](benchmarks/public/pysnooper-1/test_chinese.py)。评测语义 = 官方测试作为契约提供给模型（SWE-bench 范式），**库代码的修复由模型完成**。

> **复现公开 case**：`data/eval-cache/` 已被 `.gitignore` 忽略（第三方源码不下沉仓库）。clone 后需先 `resolve-public` 拉取 PySnooper buggy snapshot，再把上面的测试契约复制进该 snapshot 的 `tests/test_chinese.py`，然后跑 `real` 评测。具体命令见 [docs/EVALUATION.md](docs/EVALUATION.md)。

### 对结果的诚实边界

- mini 语料是 PatchProof 自持、单文件、带意图说明的 fixture，作用是**验证管道**，不证明模型修真实 bug 的能力。
- BugsInPy 5 个公开 case 中仅 pysnooper 可运行；其余 4 个（youtube-dl / black / cookiecutter / httpie）因 Python 3.6/3.7 环境与评测镜像不兼容被诚实标记 `environment_unreproducible`，**不参与评分**。
- 模型质量结论仅覆盖"小型、单文件、有测试契约"的任务；更大规模的真实修复能力需要更大语料才能下结论。

---

## 真实基建 bug 复盘

在真实评测跑通过程中，暴露了 5 个单测覆盖不到的基础设施问题（每个都可审计、可回滚）：

| # | Bug | 影响 | 修复 |
|---|---|---|---|
| 1 | Docker `--mount ...,rw` 裸布尔字段被 29.6.2 拒绝 | 容器无法启动，所有 Docker 评测/探针全挂 | `docker_executor.py` |
| 2 | 默认开启推理吃掉全部输出预算 | 任何请求 `provider_output_truncated` | `config.py` + `llm.py` 加 `PATCHPROOF_LLM_REASONING` |
| 3 | `_iter_files` 用绝对路径 parts 排除目录 | 位于 `data/` 下的仓库索引为空 | `repo_index.py` |
| 4 | **CRLF/LF 不匹配**：Windows 上 git `core.autocrlf=true` 检出，模型发出的 `old_text`（`\n`）匹配不到 `\r\n` 文件 | 所有 `apply_edit` 前置失败（"前置文本不存在"） | `workspace.py` 行尾无关匹配 |
| 5 | **时间戳不定性**：测试输出带 `HH:MM:SS`，两次门禁运行不一致 | `initial_check_nondeterministic` 拦死真实评测 | `artifact_policy.py` 归一化 ISO 时间戳 |

---

## 快速开始

后端：

```powershell
.venv\Scripts\python.exe -m uvicorn patchproof.api:app --app-dir src --reload --port 8010
```

前端：

```powershell
cd frontend
pnpm install
pnpm run dev
```

打开 `http://localhost:5175`。默认目标仓库为 PatchProof 自带的 `benchmarks/fixtures/validation` 安全 fixture。

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
  --manifest benchmarks/manifest.v2.json `
  --project-root . --output data/benchmark-real.json `
  --confirm-real --confirm-public-code-egress `
  --max-cases 5 --repeats 1 --max-requests 60 --max-tokens 400000 --max-cost-usd 2
```

Provider 配置只读自 `archive/researchflow/.env`（DEEPSEEK_* / ANTHROPIC_*），密钥不复制、不落盘、不进日志。

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
| `receipt.py` / `artifact_policy.py` | receipt 密封与篡改检测、deny 策略 |
| `frontend/` | 轻量 UI（任务、事件链、receipt、diff review） |

## 安全模型

- 执行器 `argv + shell=False`；组合命令、联网、安装、删除、Git 写操作需人工审批。
- 本地 process executor 明确标记 `local_smoke_only`，绝不冒充容器隔离。
- `apply_edit` 需要 `expected_sha256` 或精确 `old_text` 前置条件；删除不静默写回；敏感/隐藏文件默认禁编辑。
- `bug_patch.txt` 等已知答案 artifact 永不索引、不进入工作区、不暴露给 prompt/报告。

## 已知局限

- 公开语料仅 1 个 case 可运行（其余环境不兼容），模型能力结论样本小。
- mini fixture 含意图注释，适合冒烟不适合当难度基准。
- 尚未与 OpenHands / Aider / SWE-agent 做同 case 对照实验。
- 无 LSP/语义导航，代码上下文为确定性静态索引。

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)
- [docs/BENCHMARK.md](docs/BENCHMARK.md)
- [docs/DATASET.md](docs/DATASET.md)
- [docs/DOCKER.md](docs/DOCKER.md)
- [docs/EVALUATION.md](docs/EVALUATION.md)
