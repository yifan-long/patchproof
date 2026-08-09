# PatchProof v0.3.7 评测协议

PatchProof 在同一 case、同一 goal、同一源码 checkout、同一 required check、同一重复次数、
同一批全新隔离副本下，比较 one-shot baseline 与 typed tool-loop。真实路径**不使用 oracle**，
也**从不自动应用 patch**。

## 确定性 smoke 与真实路径

确定性 mini 仓库 smoke 是一个独立的基础设施测试。它的 5 个 fixture 在修复前必须失败，
且每个 fixture 只有一个仅限本地的 oracle 源码编辑。一个本来就能通过的 fixture 属于语料
错误，不是成功样本。真实路径在构造任一模型变体之前，先剥离 `expected_contents` 与断言。

追加式的运行流是规范化 JSONL。聚合 JSON 只记录解析后的指标：resolved/假完成、
required-check 与回归检查、不安全阻断、过期源码拒绝、篡改/恢复证据、时长、工具调用、
token、成本与样本数。只有当 baseline 与 harness 记录**都完整**时，一对才是
head-to-head 合格的；部分对保留在原始流中，被排除在比较率之外。

## 初始失败门禁

在构造任一模型适配器之前，PatchProof 会在两份新副本中执行解析出的 required check。
两份规范化结果必须**完全一致**且**非零退出码**。通过、超时/取消、环境失败与"两份副本
不一致"都是无效样本，**绝不进入** head-to-head 指标。共享证据包含有界、脱敏的
stdout/stderr、精确 argv、退出码、snapshot SHA-256 与证据 SHA-256。宿主路径、时长抖动与
看起来像密钥的赋值都会被归一化。

官方测试路径与失败输出指名的路径，会确定性地聚焦一个有界的源码上下文。baseline
`one_shot` 与 harness `plan` 在它们各自的行为开始之前，收到相同的 goal、check、索引、
聚焦源码、snapshot 身份与初始失败证据。

## 预算账本

每次模型请求在开始前**预留最坏情况的输出**。一个共享账本覆盖 baseline 与 harness 请求，
具有独立的 request、input token、output token 与 cost 上限。默认首轮 `$2`、扩展 `$20`。
CLI 与 API 都要求显式确认布尔值，以及有界 `max_cases`、`repeats`、`max_requests`、
`max_tokens` 与 `max_cost_usd`。

## Provider 传输方式（transport）

Provider transport 是显式的。Anthropic 兼容配置使用 messages API。`DEEPSEEK_*` 与
DeepSeek 模型使用 OpenAI 兼容的 `chat.completions`，带 system/user 消息与 JSON-object
响应格式。对于免凭据的 HTTPS 根 `https://opencode.ai` 与已知的 `deepseek-v4-flash` 模型，
`opencode_plan=go` 解析到 `/zen/go/v1`，`opencode_plan=zen` 解析到 `/zen/v1`。`auto`
根 URL 有歧义，**失败关闭（fail closed）**。已经是显式的 `/zen/go/v1` 与 `/zen/v1` URL
是权威并被保留，任意自定义 HTTPS 主机与路径同样如此。

非密钥的账户选择可以存储在被 gitignore 的 PatchProof 本地 `.patchproof.local.env`；
该文件只读取 `PATCHPROOF_OPENCODE_PLAN`。凭据、模型与 base URL 来自 PatchProof 本地的
`.env`（若存在），或来自显式提供的 provider 文件。优先级是进程 `PATCHPROOF_*`、本地
profile、显式构造参数、然后是本地 provider 文件的 `OPENCODE_PLAN`。Preflight 暴露解析后
的 profile、transport、主机与 base path——**不暴露凭据**。

Provider 异常会取消未决的预算预留；成功的响应即使内容是非合法 JSON，也会**恰好一次**
提交观测到的 prompt/completion 用量。

## Baseline one-shot 基线

baseline 恰好是一次模型请求，**没有工具、没有迭代反馈**。它偏好的响应是一个有界紧凑
替换，包含仓库相对 `path`、非空 `old_text`、`new_text`，以及可选的当前文件
`expected_sha256`。PatchProof 要求 `old_text` **恰好字节一致地匹配一处**；缺失、歧义或
过期的前置条件被拒绝，**不做模糊匹配**。全文件 `new_text` 响应仍被支持，用于新文件与
确定性兼容，但真实 prompt 禁止复制未改动或整份文件，除非必要。证据只记录 path、编辑
模式与前置条件种类——**不记录源码片段**。

OpenAI 兼容的完成原因 `length`（以及等价的 `max_tokens`）与 Anthropic 的 stop reason
`max_tokens` 被分类为 `provider_output_truncated`。因为这些都是成功的 provider 响应，
它们的观测用量会被提交一次，任何预留都会在产生安全失败信封之前被清除。
没有截断原因的非合法 JSON 仍是 `provider_invalid_json`。

## 操作失败产物

真实 CLI 把预期的 provider、配置与硬预算失败当作**操作结果**处理。它以
`patchproof.real-evaluation-failure.v1` 原子写入请求的输出、打印简洁 JSON、以状态码 2
退出且**无 traceback**。信封包含脱敏的 provider 元数据、预算阶段/上限/当前账本、选中的
case IDs 与重复次数、时间戳，以及安全的失败类别/消息。它**排除** provider 响应体、API
key、账单链接、源码上下文与模型质量指标。

不完整的对**不产生** head-to-head 结果。pair 内部的 provider 失败不会追加该 pair 的任何
变体。硬预算耗尽时，已经观测到的部分记录可能以显式 `partial` 状态留在追加式 JSONL 中，
但失败信封只报告它们的数量并标记它们**不可比较**。先前完成的 pair 在失败前计为证据；
它们不会被表示为"失败 pair 已完成"。

## 先离线命令

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark smoke --manifest benchmarks/manifest.v2.json --output data/benchmark-v03-mini.json
.venv\Scripts\python.exe -m patchproof.faults run --output data/fault-report.json
```

`--confirm-real` 意味着可能发生 provider 调用。`--confirm-public-code-egress` 意味着在
resolver preflight 之后可能发生公开源码外发。两个标志都不能覆盖缺失的 Docker daemon、
未解析的 provenance、镜像 pin 失败或硬预算。

公开解析是一个**独立的非模型操作**。真实评测接受一个已解析的锁 manifest，但如果任一
选中的 case 未解析、有浮动镜像身份、或在 pin 的 evaluator 中缺少
`executable_state=verified_failing`，就仍然保持 blocked。Resolver 输出显式记录
`model_calls=0` 与 `public_code_llm_egress=false`；它包含 provenance，**不包含**评测分数。

已知答案产物——包括 BugsInPy 的 `bug_patch.txt`——被一个**中央策略拒绝**。它们被排除在
任务语义哈希、物化工作区、仓库索引、typed 读取、diff 报告与 prompt 之外。Resolver 只记录
"某个产物被排除"，**不序列化**它的路径或内容。

## 故障矩阵

`patchproof.faults` 中的可执行 hook 覆盖：任意命令完成、检查后编辑证据失效、旧 HEAD/源、
脏工作树、非法工具、路径穿越/受保护路径、风险命令、超时/输出洪水/取消、重启中断、
事件篡改、receipt/artifact 篡改与预算耗尽。manifest 是期望契约；runner 是可测试的实现。

## 复现公开 BugsInPy 案例

三个 BugsInPy 公开 case 在 pin 的 Python 3.8 evaluator 上是 `verified_failing`，并形成
完整的 baseline-vs-harness 对：`pysnooper-1`、`pysnooper-3`、`fastapi-1`。另外四个公开
描述符是 `environment_unreproducible`（Python 3.6/3.7 运行时与 Python 3.8 evaluator 不匹配），
并诚实地被排除在评分之外。

复现所报告结果的步骤：

```powershell
# 1. 构建 pin 的 evaluator 镜像（Python 3.8 基础 + pytest + 官方测试需要的运行时依赖）
#    并解析官方快照。requirements.lock 安装 pytest、future、decorator、six、fastapi、
#    pydantic、starlette —— 全部 pin。
.venv\Scripts\python.exe -m patchproof.benchmark build-evaluator-image `
  --base-image "python:3.8.20@sha256:<verified-64-hex-digest>" `
  --tag patchproof-evaluator:0.3.8-fixes --output data/evaluator-image.lock.json --confirm-build
.venv\Scripts\python.exe -m patchproof.benchmark resolve-public `
  --manifest benchmarks/public/bugs-in-py.v2.json `
  --image-lock data/evaluator-image.lock.json `
  --output data/bugs-in-py.resolved.v3.lock.json --confirm-download

# 2. 官方测试文件依赖 evaluator 镜像未安装的 python_toolbox / 其他包，所以把自包含的
#    重建测试契约（保存在仓库内 benchmarks/public/ 下）物化到解析后的快照中。
#    fastapi-1 的官方检查已经直接指向重建的测试名。
#    快照：  pysnooper-1 -> data/eval-cache/sources/<e21a311-hash>
#            pysnooper-3 -> data/eval-cache/sources/<6e3d797-hash>
#            fastapi-1   -> data/eval-cache/sources/<766157b-hash>
$s = Get-ChildItem data/eval-cache/sources -Directory
Copy-Item benchmarks/public/pysnooper-3/test_file_output.py `
  ($s | Where-Object { Test-Path "$($_.FullName)\pysnooper\pysnooper.py" } | Select-Object -First 1).FullName
Copy-Item benchmarks/public/fastapi-1/test_jsonable_encoder.py `
  ($s | Where-Object { Test-Path "$($_.FullName)\fastapi" } | Select-Object -First 1).FullName

# 3. 运行真实的 baseline-vs-harness 比较。每个 case 在共享账本下运行；
#    必要时用全新的预算单独运行困难 case。
.venv\Scripts\python.exe -m patchproof.benchmark real `
  --manifest data/bugs-in-py.resolved.v3.run.lock.json `
  --project-root . --output data/benchmark-real-bugsinpy-v3.json `
  --confirm-real --confirm-public-code-egress --confirm-download `
  --max-cases 3 --repeats 1 --max-requests 100 --max-tokens 600000 --max-cost-usd 4
```

复现只物化测试契约；库修复（`pysnooper/tracer.py`、`pysnooper/pysnooper.py`、
`fastapi/encoders.py`）由模型产生。`data/eval-cache/` 被 gitignore，所以重新 clone 后
必须重做步骤 2。报告的结果：三个 case 都是完整对。预算提示：使用
`PATCHPROOF_LLM_REASONING=off`；对于大型 one-shot patch，使用
`PATCHPROOF_MAX_TOKENS=16384` 以避免 `provider_output_truncated`。

一个在 `fastapi-1` 上发现的 one-shot 聚焦源码弱点已在 `evaluation.py` 与 `repo_index.py`
中修复：当失败输出指名一个函数但**没有**给出文件路径时，该符号的定义文件现在会被加入
聚焦上下文（整词匹配），且聚焦文件总是被分配源码槽位。
