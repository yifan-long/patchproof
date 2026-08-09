# Windows 本地演示

`demo.cmd` 是完整的本地 PatchProof 演示在 Windows 上的双击入口。它准备依赖、在后台启动
API 与 UI、等待两个服务都变健康，然后打开 `http://localhost:5175`。

## 前提

- Windows 10 或更新版本。
- `PATH` 上有 [uv](https://docs.astral.sh/uv/)。
- `PATH` 上有 Node.js 与 pnpm。

启动器**不安装系统软件**。每次启动它都会安全地运行 `uv sync` 与
`pnpm install --frozen-lockfile`；两个命令都由 lockfile 驱动且幂等。

## 首次运行

双击 `demo.cmd`，或运行：

```powershell
.\demo.cmd
```

首次运行会询问 provider base URL、模型、transport 与 API key。key 输入是**掩码**的。
启动器随后把后端绑定到 `127.0.0.1:8010`、前端绑定到 `127.0.0.1:5175`，不使用
uvicorn reload。

对于自动化或脚本化安装，把 key 读成 `SecureString`，避免让它留在命令历史里：

```powershell
$key = Read-Host 'API key' -AsSecureString
.\deploy\local-demo.ps1 configure `
  -BaseUrl 'https://provider.example/v1' `
  -Model 'your-model' `
  -Transport 'openai-compatible' `
  -ApiKey $key
.\deploy\local-demo.ps1 start -NoBrowser
```

不要把明文 API key 写进命令、脚本、截图或 issue 报告。

## 命令

| 命令 | 用途 |
|---|---|
| `.\demo.cmd` 或 `.\demo.cmd start` | 准备并启动；健康检查后打开浏览器 |
| `.\demo.cmd configure` | 替换 provider 配置与加密 key |
| `.\demo.cmd status` | 显示已验证的后端/前端进程状态 |
| `.\demo.cmd logs` | 打印每个本地日志的最后 80 行 |
| `.\demo.cmd stop` | 停止已验证的后端/前端进程树 |
| `.\deploy\local-demo.ps1 start -NoBrowser` | 不打开浏览器启动，适合自动化 |

更换正在运行的后端所用 provider 时，先运行 `stop` 再 `configure`，然后重新 `start`。

## 密钥与进程安全

- `deploy/.local-demo.config.json` 以明文存储 base URL、模型与 transport。它的 API key
  字段直接用 Windows DPAPI `CurrentUser` 加密，并存储为 Base64 密文，绑定到当前 Windows
  用户，同时在 Windows PowerShell 5.1 与 PowerShell 7 之间可互操作。
- 配置与所有运行时状态都被 Git 忽略。DPAPI 加密**不可移植**到另一个 Windows 用户，
  也**不保护**同一已登录账户被攻破的情况。
- 启动时，key 只被解密到足以放进后端子进程环境的时长。父进程环境被立即恢复；前端在其后
  启动，**从不接收 key**。
- UI 只持久化非密钥的 provider 字段。它会丢弃旧版 PatchProof 遗留的 `api_key`
  （而不是从 `localStorage` 恢复）。
- 日志、PID 与进程元数据位于 `deploy/.local-demo/`。状态文件**从不包含** provider 凭据。
- `stop` 在调用 `taskkill /T` 之前校验 PID、可执行路径与精确的进程启动时间。
  被复用或过期的 PID 会被报告并跳过。

只要 demo 在运行，key 就必然存在于后端进程内存/环境中，因为后端需要创建模型客户端。
不要在共享或不可信的 Windows 账户下运行 demo。

## 故障排查

**缺少必需工具。** 自行安装指定的工具，重开终端让 `PATH` 刷新，然后重试。启动器
从不执行系统级安装。

**8010 或 5175 端口被占用。** 启动器拒绝接管未跟踪的监听器。停止冲突的应用，然后重新
运行 `start`。

**配置无法解密。** 以将要运行 demo 的 Windows 用户运行 `configure`。复制的配置文件无法
在另一个用户上下文下解密。

**服务在启动期间退出。** 运行 `.\demo.cmd logs`。依赖输出保留在启动控制台；服务
stdout/stderr 存储在 `deploy/.local-demo/` 下。

**状态显示降级或过期。** 运行 `.\demo.cmd stop`；身份匹配的进程会被停止，不匹配的 PID
会被单独留下。然后重新 `start`。

这个启动器用于**本地演示**。Linux Docker + Caddy 部署记录在
[DEPLOYMENT.md](DEPLOYMENT.md)。
