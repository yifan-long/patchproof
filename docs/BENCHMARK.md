# 评测基准 Harness（v0.3.7）

PatchProof 有两套**刻意不同**的评测模式。

## 确定性 smoke：基础设施验证，而非模型质量比较

确定性 smoke 是**基础设施验证**，不是模型质量比较。每个 mini fixture 都是故意损坏的。
在产出任何结果之前，smoke 会先运行要求的检查命令，并且**拒绝"本来就能通过"的 fixture**
（已经通过的 fixture 属于语料错误，不是成功样本）。每个 case 恰好包含一个仅限本地的
全文件 oracle 编辑。baseline 通过 snapshot 工作区应用它；FakeLLM harness 必须精确执行
`apply_edit` → `run_check` → `finish`。修复后两个变体都必须通过，报告期望的改动文件，
并产生非空 patch。稳定的 v0.3.2 期望值是：10 次成功运行、每次运行改动 1 个文件、
patch 大小非零、每个 case 恰好 3 次 harness 工具调用。时长与字节大小仍作为观测值记录。

## 可选的 real 模式：成对 head-to-head 比较

可选的 real 模式在**两份全新隔离副本**上，用同一 case、同一 goal、同一个
`check_command` 进行比较：

- `baseline_one_shot_real`：恰好一次真实模型调用，默认返回有界紧凑精确替换
  （全文件编辑仍是兼容模式）；结果只在其副本内被检查，**绝不写回源仓库**。
- `harness_tool_loop_real`：真实 plan/action/observation 调用走 typed 循环、策略门禁、
  编辑前置条件、修复预算与 Patch Receipt；它也**绝不调用 Apply**。

Real 模式收到的是**剥掉 oracle 的 case 视图**，永远读不到 `expected_contents`——该字段
只是确定性 fixture 的 oracle。real 模式要求显式 case 上限与预估成本上限。成本估算在
无法进行 token 记账时是保守的，且**不是** provider 的账单。

紧凑编辑要求 `old_text` 非空，且在当前文件中**恰好匹配一处**。可选的 SHA-256 增加
"当前文件字节精确一致"的前置条件。缺失、歧义、过期、超出策略、过大或过多的编辑都会被
拒绝；证据中不会出现模糊匹配或源码片段。provider 的 token 上限导致的完成原因会产生
`provider_output_truncated`（而不是 `provider_invalid_json`），且不会产生部分的
head-to-head 声明。

版本化语料 manifest 是 `benchmarks/manifest.v2.json`。它包含 5 个本地 case，使用严格的
`BenchmarkCase` v2 字段：源码种类、argv 安装与必查命令、镜像/资源策略、允许/期望路径、
重复次数与 provenance。公开未解析描述符单独保存在 `benchmarks/public/bugs-in-py.v2.json`。

```powershell
cd C:\Users\Administrator\Desktop\简历项目\patchproof
.venv\Scripts\python.exe -m patchproof.benchmark smoke `
  --manifest benchmarks/manifest.v2.json `
  --project-root . `
  --output data/benchmark-smoke.json
```

可选的真实模型入口要求显式 `--confirm-real`，公开 case 还要
`--confirm-public-code-egress`。它还要求 Docker preflight 与已解析的不可变公开
provenance。provider 调用与成本是真实的；默认是 `$2` 首轮、`$20` 扩展预算。
请在可复现的命令中把所有上限都保持显式。

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark real `
  --manifest benchmarks/manifest.v2.json `
  --project-root . `
  --output data/benchmark-real.json `
  --confirm-real --confirm-public-code-egress --max-cases 1 `
  --repeats 1 --max-requests 40 --max-tokens 32768 --max-cost-usd 2
```

## 报告模板

下面数字在完成可复现运行之前**有意留空**。

| 变体 | 成功率 | 平均步数 | 平均工具调用 | 平均时长 | 平均改动文件数 | 平均 patch 大小 | 审批数 | required-check | receipt 文件 | 事件链 | 前置条件 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline_one_shot_real | — | — | — | — | — | — | — | 不适用 | 不适用 | 不适用 | — |
| harness_tool_loop_real | — | — | — | — | — | — | — | — | — | — | — |

报告额外保留精确的 `goal`、`check_command`、改动路径、失败类别、命令退出码、模型用量、
审批次数、逻辑 receipt 校验、receipt artifact 文件校验与事件链校验。
本模板中**没有任何数字是已声明的结果**。
