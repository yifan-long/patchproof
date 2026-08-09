# PatchProof 威胁模型

## 资产

- 用户的源仓库与未提交的编辑。
- 从现有环境加载的 provider 凭据。
- "任务已测试、可以安全 Apply"这一声明。
- 评审期间使用的 Receipt 与事件证据。

## 信任边界

1. **模型是不受信任的输入**。它的 JSON 输出按六个 typed tool 校验。
2. **staging 工作区与源仓库隔离**。
3. **Docker 执行是公开/真实评测的必要边界**。本地进程执行器只是一个显式标注的离线
   smoke 路径，仍拥有操作系统用户的权限。
4. **SQLite 是可持久化存储，不是 write-once ledger**。哈希链验证检测篡改；
   它不能阻止特权用户改库。
5. **人是风险命令与 Apply 的最终裁决者**。

## 安全不变量

- 无 `shell=True`；执行使用 argv 列表与 `shell=False`。
- Docker 执行拒绝浮动镜像、privileged 模式、Docker socket 挂载与网络访问；
  缺失 daemon 状态会阻断公开/真实运行。
- Shell 组合、网络访问、安装、删除与 Git 写入**不会**被自动批准。
- typed 编辑必须提供 `expected_sha256` 或 `old_text`；路径在 staging root 内解析，
  敏感文件被阻止。
- 紧凑 one-shot 编辑额外要求非空、唯一、精确的 `old_text` 匹配。可选哈希对当前文件字节
  校验；写入是原子且有界的，**没有模糊匹配**。
- BugsInPy 的 patch/fix/oracle 产物被拒绝进入源码上下文、物化的评测副本、typed 读取、
  prompt 与报告。Resolver 的任务语义只使用保守解析的官方元数据与一条安全的
  `run_test.sh` 命令。
- 公开对要求来自**两个 pin 的 Docker 副本**的匹配非零 fail-before 证据，之后才构造模型。
  通过、不一致、不可运行、运行时不相容或未校验的检查被排除在评分之外。
- `check_command` 只归一化一次。只有**来自当前编辑代际**的成功精确-argv required check
  才能授权 `finish(verified)`；之后的编辑使该证据失效。
- Apply 对照记录的 baseline 检查原始 HEAD/状态/manifest。
- 删除不会静默写回。
- 进程重启把运行中的任务标记为 `interrupted` 并追加一条事件。
- 单独的成功测试**不是**完成声明：在进入 `awaiting_apply` 之前，要求 `finish(verified)`
  与 Patch Receipt。
- 规范化 Receipt artifact 原子写入，其真实文件字节记录在 SQLite 中；逻辑哈希或文件篡改
  都可检测。
- 确定性 mini 仓库 smoke 要求记录 fail-before 状态，并且只使用恰好一个本地 fixture 编辑
  来做基础设施验证。真实评测收到剥掉 oracle 的 case，两个变体都使用模型产生的编辑，
  并要求显式确认、共享硬预算、不可变 provenance，且不自动 Apply。部分对会保留，
  但被排除在 head-to-head 比率之外。

## 明确限制

本地进程执行器不是 Docker、VM 或内核级沙箱。人只应批准自己理解的命令，且生产部署凭据
不应出现在 PatchProof 进程环境中。Docker daemon/镜像就绪与公开 provenance 是外部
preflight 要求；缺失时本工作区**不声称**具备它们。
