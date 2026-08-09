# Docker 评测隔离

`DockerEvalExecutor` 是一个可注入的执行层。测试用 fake runner 断言它的精确 argv；
生产执行与构建/安装网络**相互分离**，且**不能静默回退**到本地进程执行器。

执行容器使用：

- digest 固定的镜像；
- `--read-only` rootfs + 一个隔离的可写 `/workspace` bind 挂载；
- `--network none`、确定性 `TZ`、locale 与 `PYTHONHASHSEED`；
- CPU、内存、PID、超时、取消与输出限制；
- `no-new-privileges`、`--cap-drop ALL`、无 privileged 模式、无 Docker socket。

构建/安装命令使用**单独配置的显式网络模式**。它们产出安装证据，并且不会改变执行命令的
`none` 网络。daemon **从不被自动重配置**。

Preflight 报告 CLI 可用性、daemon/版本、镜像 pin 与可用性、缓存状态、显式
registry/ACR 主机、包镜像主机，以及**不含密钥**的执行模式。`local://patchproof-python312`
标记意味着 `local_smoke_only`；**它不是 Docker 隔离声明**。

国内镜像策略是**纯配置**的：可以为 pin 的镜像提供显式 registry/ACR 映射，而 TUNA 镜像
仅限于 apt/pip/package 安装命令。PatchProof **从不修改 Docker daemon 配置**。

受控的 evaluator 镜像只由显式命令构建，使用 digest 固定的基础镜像：

```powershell
.venv\Scripts\python.exe -m patchproof.benchmark build-evaluator-image `
  --base-image python@sha256:<verified-64-hex-digest> `
  --output data/evaluator-image.lock.json `
  --confirm-build
```

构建器使用 `--pull=false`，**从不请求 privileged 模式或挂载 Docker socket**，并 inspect
生成的镜像。它的带校验和锁清单记录不可变的本地 `sha256:` 镜像 ID、仓库 digests、
Dockerfile 与依赖锁的哈希，以及安全的构建策略。直接使用 Docker 镜像 ID 是 evaluator
接受的显式不可变引用。

构建还会在完成的镜像内部以只读 rootfs 与 `--network none` 探测
`platform.python_version()`；验证过的运行时写入镜像锁。公开解析在运行官方检查前把它的
主/次版本与 BugsInPy 元数据比较。不匹配就是 `runtime_version_mismatch`，**不是**已解析
可执行 case。PatchProof **永不回退**到宿主机解释器，也**不会静默拉取**旧的浮动镜像。

`--acr-registry` 只改变本地镜像名。TUNA 仅被接受为包镜像，例如
`--pip-index-url https://pypi.tuna.tsinghua.edu.cn/simple`；两个选项都不会登录 registry，
也不会修改 daemon 配置。

当 daemon 不可用时，公开与真实评测报告为 **blocked**。离线 mini 仓库 smoke 与故障注入
hook 仍可作为本地 smoke 运行，并带有上面的 `local://patchproof-python312` 标记。
