# PatchProof v0.3.7 数据集与溯源（Dataset & Provenance）

本地语料包含 5 个自包含、故意失败、PatchProof 自有的 mini 仓库，位于
`benchmarks/fixtures/`。它们**不需要网络**。每个 manifest 条目恰好有一个固定的全文件
`expected_contents` oracle，其路径与 `expected_changed_files` 完全一致：

| case | 语义焦点 |
| --- | --- |
| `mini-validation` | 校验与归一化 |
| `mini-config-precedence` | 显式 env/文件/默认值优先级 |
| `mini-pagination` | 分页边界行为 |
| `mini-idempotency` | 幂等状态转换 |
| `mini-serialization` | 向后兼容的序列化 |

非本地 case 会拒绝 oracle 字段。确定性 smoke 记录失败的 required check，然后只为本地
baseline 与 FakeLLM 基础设施路径使用 oracle。真实/公开评测收到的是**剥掉 oracle 字段的
case 副本**。

## 公开描述符

公开描述符位于 `benchmarks/public/bugs-in-py.v2.json`。它们只使用官方 BugsInPy 标识符与
官方元数据 URL：

| 项目 | bug | 官方标识符 |
| --- | ---: | --- |
| youtube-dl | 2 | `bugsinpy-checkout -p youtube-dl -v 0 -i 2` |
| PySnooper | 1 | 官方 BugsInPy 项目/bug 树 |
| PySnooper | 3 | 官方 BugsInPy 项目/bug 树 |
| fastapi | 1 | 官方 BugsInPy 项目/bug 树 |
| black | 1 | 官方 BugsInPy 项目/bug 树 |
| cookiecutter | 1 | 官方 BugsInPy 项目/bug 树 |
| httpie | 1 | resolver 门控的官方项目/bug 树 |

全部 5 个源码描述符的 `provenance_state=unresolved`。它们**有意不声明**上游 commit、
源码许可证或镜像 digest。在公开评测可以继续之前，resolver 必须获取并校验这些值：
校验 checkout HEAD、解析 SPDX 许可证与不可变镜像 digest、记录机器可读证据。
`resolve-public` 会写入一个独立的规范化 `patchproof.public-lock.v1` manifest；
它**永不修改描述符**。第三方源码按数据集 URL/修订版本或源码 URL/commit 缓存到被
gitignore 的 `data/eval-cache/`；**绝不 vendoring 进仓库**。

解析是显式的，且**不调用模型、不把公开代码发给 LLM**：

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark resolve-public `
  --manifest benchmarks/public/bugs-in-py.v2.json `
  --image-lock data/evaluator-image.lock.json `
  --output data/bugs-in-py.resolved.lock.json `
  --confirm-download
```

一个已验证过的官方数据集 checkout 可以**不联网重放**：使用 `--dataset-root` 连同完整的
`--dataset-revision`；源码缓存仍会经过 detached-HEAD 校验。这些选项不会削弱显式确认、
镜像运行时或"失败检查"要求。

resolver 会钉住官方 BugsInPy 数据集修订版本、读取其 `project.info` 与 `bug.info`、
校验精确的 buggy checkout HEAD、对元数据与 checkout 内容做哈希、并保守地识别许可证证据。
缺失、冲突、不安全或不可复现的证据会产出一个结构化的 unresolved 原因。锁清单只把
固定 commit 记为官方任务身份；**绝不把它暴露为修复 oracle**。

## 任务语义

对于任务语义，v0.3.7 从官方 bug 目录要求 `python_version`、`test_file` 与 `run_test.sh`。
`run_test.sh` 必须恰好包含一行非空命令，且 token 化后是一个被批准的 argv，**不含**
shell 可执行文件、组合、重定向或环境赋值。解析出的 goal 只提及官方项目/bug 与失败测试
身份。锁清单为 `project.info`、`bug.info` 与 `run_test.sh` 记录 SHA-256 证据；
它**从不从 `bug_patch.txt` 推导语义**。

一个公开 case 只有在数据集元数据、完整 buggy 与 fixed commits、checkout HEAD、
许可证/SPDX 证据、evaluator 镜像锁**全部可校验**时才变为 resolved。它还必须在一个
探测到 Python 主/次版本与官方元数据一致的镜像里复现出**非零官方检查**。通过的测试、
缺失依赖、不安全命令、运行时不匹配或 Docker 探测不可用都会保持 unresolved。
`expected_contents` 与断言在公开锁清单中**始终为空**。

## 已知答案产物的排除

`bug_patch.txt` 与已知的 patch/fix/oracle 变体**在读取其内容之前就被排除**。
它们不会被复制进 probe/评测工作区，也无法通过 PatchProof 工具读取。

官方数据集的署名与元数据来源是 manifest 中链接的 BugsInPy GitHub 仓库与 README。
测试使用本地伪造的官方元数据与本地 Git 仓库；preflight **从不下载源码**。
