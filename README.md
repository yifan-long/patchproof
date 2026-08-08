<p align="center">
  <img src="docs/hero.svg" alt="PatchProof — evidence before Apply" width="100%">
</p>

<div align="center">

# PatchProof

**让 AI 编码 Agent 证明它真的改完了。** 不是再相信一次模型自述，而是审阅一条可复核的完成证据链。

[![version](https://img.shields.io/badge/version-v0.3.7-111827)](pyproject.toml)
[![project python](https://img.shields.io/badge/project%20Python-%E2%89%A53.12-0f766e)](pyproject.toml)
[![evaluator](https://img.shields.io/badge/BugsInPy%20evaluator-3.8.20-334155)](docker/evaluator/Dockerfile)

[Windows 双击演示](#windows-本地一键演示) · [Linux 自托管](#linux-一键自托管) · [实测证据](#实测证据) · [架构与安全](#架构与安全) · [完整文档](#文档导航)

</div>

PatchProof 把 Agent 放进隔离工作区，只开放 6 个类型化工具；每次动作、观察与审批都进入 SHA-256 事件链。只有在最后一次编辑之后，**逐 argv 匹配并跑通任务指定的验证命令**，它才能生成 Patch Receipt。最终 diff 始终由人审阅，**永不自动 Apply**。

| 已验证的公开样本 | 完整证据对 | 自动 Apply | 证据出口 |
|---:|---:|---:|---|
| 3 个 BugsInPy case | 3 / 3 | **0 次** | diff + required-check + receipt + event chain |

> 这些数字描述的是当前仓库内可追溯的评测结果，不是通用模型能力声明。一个更复杂的公开样本 `pysnooper-2` 在预算内失败，结果被保留而非重跑美化。

## Windows 本地一键演示

已安装 [uv](https://docs.astral.sh/uv/)、Node.js 和 pnpm 的 Windows 10/11 用户，可直接双击根目录的 **`demo.cmd`**。首次运行会安全询问模型端点和 API key、同步锁定依赖、启动前后端，健康检查通过后打开 `http://localhost:5175`。

```powershell
.\demo.cmd
```

API key 使用当前 Windows 用户的 DPAPI 加密保存在 Git 忽略的本地配置中，不进入命令行、日志或前端 `localStorage`。常用命令为 `.\demo.cmd status`、`logs`、`stop` 与 `configure`；安全设计和故障排查见 [Windows 本地演示指南](docs/LOCAL_DEMO.md)。

## 工作流

<p align="center">
  <img src="docs/proof-flow.svg" alt="PatchProof evidence flow and trust boundaries" width="100%">
</p>

1. **隔离** — 在 detached worktree 或 snapshot 中工作，不直接改源仓库。
2. **受控行动** — 模型只能搜索、读取、编辑、查看 diff、运行检查、声明完成；编辑必须带精确旧片段或文件哈希。
3. **强制验收** — 只有任务创建时指定的那条命令，在最近一次编辑后真实成功，才取得完成资格。
4. **密封证据** — 计划、工具记录、文件前后哈希、diff 哈希、测试退出码与审批轨迹写入 Patch Receipt，并与事件链相互校验。
5. **人工裁决** — 进入 `awaiting_apply` 后暂停；你审 diff、看证据，再决定是否写回。

请求数、token 与费用在每次模型调用前按最坏情况预留，并受共享硬预算约束。预算耗尽、环境不可复现、输出截断、编辑前置条件失败都会得到明确分类，不会伪装成成功。

## 实测证据

### BugsInPy 公开案例

3 个案例通过了 baseline one-shot 与 harness tool-loop 的成对评测；公开/真实评测运行在固定 digest 的 Docker 镜像中，禁用网络、使用只读 rootfs，且不挂载 Docker socket。

| case | 缺陷 | baseline | harness | tool-loop 步数 |
|---|---|---:|---:|---:|
| `pysnooper-1` | 中文源码被 `ascii` 解码 | 通过 | 通过，`awaiting_apply` | 7 |
| `pysnooper-3` | 文件输出引用未定义的 `output_path` | 通过 | 通过，`awaiting_apply` | 3 |
| `fastapi-1` | `jsonable_encoder` 缺少 `exclude_defaults` | 通过 | 通过，`awaiting_apply` | 7 |

两份隔离副本在模型构建前都必须产生一致的非零失败；通过、不可运行、环境不兼容或失败不一致的样本不会进入评分。`pysnooper-2` 涉及 `custom_repr` 跨 3 处接线，模型在共享预算内未完成，按 `llm_budget_exhausted` 如实记录且不计入 3 / 3。

**Provenance。** BugsInPy 上游 PySnooper / FastAPI 测试依赖评测镜像未安装的库，本仓库因此保存自包含的重建测试契约，逐断言验证 buggy 版本失败、官方修复后通过。测试契约可见于 [`benchmarks/public/`](benchmarks/public/)；库代码修复仍全部由模型完成。第三方源码快照不下沉仓库，公开案例的解析、镜像构建与复现步骤见 [评测指南](docs/EVALUATION.md)。

**运行时边界。** PatchProof 项目本身要求 **Python ≥3.12**；BugsInPy evaluator 为兼容被测项目固定在 **Python 3.8.20**。这是两个不同运行环境，不代表主项目支持 Python 3.8。

### 自持 smoke 语料

5 个 mini fixture 用于验证管道，而非衡量真实修复难度。它们全部达到 `awaiting_apply`，且 `required_check_verified`、receipt 文件与事件链校验均通过，`precondition_failures == 0`。

`validation` · `config-precedence` · `pagination` · `idempotency` · `serialization`

## 手动开发启动

需要 Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js 与 pnpm。后端：

```powershell
uv sync
uv run uvicorn patchproof.api:app --app-dir src --port 8010
```

另开终端启动前端：

```powershell
cd frontend
pnpm install
pnpm run dev
```

打开 `http://localhost:5175`；默认目标是仓库内安全 fixture `benchmarks/fixtures/validation`。

> Windows 下不要添加 `uvicorn --reload`。reload worker 可能使用 `SelectorEventLoop`，使 `asyncio.create_subprocess_exec` 抛出 `NotImplementedError`；后端已在非 reload 模式显式设置 Proactor 策略。

### 验证管道

不调用模型的确定性 smoke suite：

```powershell
uv run python -m patchproof.benchmark smoke `
  --manifest benchmarks/manifest.v2.json --project-root . `
  --output data/benchmark-smoke.json
```

安全钩子故障注入：

```powershell
uv run python -m patchproof.faults run --output data/fault-report.json
```

公开模型评测需要显式确认、固定镜像与预算上限；完整准备和复现命令见 [docs/EVALUATION.md](docs/EVALUATION.md)。Web 任务可由浏览器按任务提供 provider，API key 只在运行期内存中短驻且不会落库或进入日志；CLI 评测也支持显式环境配置。

## Linux 一键自托管

Linux 服务器已安装 Docker Engine + Compose v2，且域名已解析到服务器时，在仓库根目录运行：

```bash
bash deploy/deploy.sh patchproof.example.com
```

Caddy 自动配置 HTTPS，域名同时驱动后端 CORS。没有域名时可用 `bash deploy/deploy.sh --localhost` 启动仅监听本机的 HTTP 冒烟环境。脚本还提供 `status`、`upgrade`、`logs` 与 `uninstall`；持久数据保存在 `deploy/data/`，任务仓库放在 `deploy/repositories/<name>`。

部署脚本不读取或写入 LLM key，用户在浏览器中自带 key。默认 Compose 也不挂载宿主机 Docker socket，因此 Web 部署本身不获得公开评测所需的 Docker 隔离能力；相关任务会保持 blocked，不会降级成本地执行器冒充隔离。详见 [部署文档](docs/DEPLOYMENT.md)。

## 架构与安全

```text
Vue Evidence Console ── FastAPI / SSE ── TaskManager ── durable state machine
                                              │
                    ┌─────────────────────────┼──────────────────────┐
                    ▼                         ▼                      ▼
             typed agent loop          SQLite event chain     Patch Receipt
                    │
          policy gate + workspace strategy
                    │
            Docker evaluator / explicit local_smoke_only
```

核心不变量：

- **模型不受信任**：输出先通过严格 schema，只能调用有限工具；执行使用 argv 与 `shell=False`。
- **工作区有边界**：路径必须落在 staging root，敏感/隐藏文件默认禁止编辑，删除不会静默写回。
- **检查不可替换**：任意“看起来成功”的命令不能替代原始 `check_command`，后续编辑会让旧验证失效。
- **证据可检验而非不可破坏**：SQLite 不是 write-once ledger；SHA-256 链和 receipt 文件哈希负责暴露篡改，不能阻止特权用户改库。
- **Apply 属于人**：风险命令需要审批，写回前还会复核原 HEAD、工作树与 manifest。
- **本地执行器不是沙箱**：`local_smoke_only` 仍继承操作系统用户权限；公开/真实评测缺少 Docker 前置条件时必须阻断。

模块职责、状态转换与 threat model 分别见 [架构文档](docs/ARCHITECTURE.md) 和 [威胁模型](docs/THREAT_MODEL.md)。

## 诚实边界

- 官方结果只覆盖 3 个进入计分的真实案例，涉及 PySnooper 与 FastAPI；不能外推到大型、多语言或长期任务。
- `pysnooper-2` 是保留的预算内失败样本；失败不重跑、不从报告中抹除。
- 自持 mini fixture 含意图说明，只适合基础设施冒烟，不是难度 benchmark。
- 尚未与 OpenHands、Aider、SWE-agent 做同案例对照。
- 上下文来自确定性静态索引，尚无 LSP / 语义导航。
- 公开语料还受上游源码、固定运行时、Docker daemon 与本地 cache 的可复现性约束。

真实评测打通时还发现并修复了 **6 个**单元测试未覆盖的基础设施问题：Docker mount 参数兼容、默认推理挤占输出预算、绝对路径目录排除、CRLF/LF 精确编辑、时间戳导致初始检查不确定，以及 one-shot 聚焦上下文漏掉仅以符号名出现的文件。具体证据与失败分类见 [评测指南](docs/EVALUATION.md) 和测试目录。

## 文档导航

| 文档 | 内容 |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | 状态机、模块边界、receipt 与事件链 |
| [Threat Model](docs/THREAT_MODEL.md) | 信任边界、安全不变量、明确限制 |
| [Evaluation](docs/EVALUATION.md) | 公开案例准备、运行与复现 |
| [Benchmark](docs/BENCHMARK.md) | smoke / real 模式与报告结构 |
| [Dataset](docs/DATASET.md) | 语料 provenance 与解析策略 |
| [Docker](docs/DOCKER.md) | evaluator 镜像与隔离要求 |
| [Windows local demo](docs/LOCAL_DEMO.md) | DPAPI 配置、本地启动、日志与停止 |
| [Deployment](docs/DEPLOYMENT.md) | 自托管、运维和部署边界 |
