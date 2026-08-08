# PatchProof 一键自托管部署

PatchProof 的一键部署目标是：在一台 Linux 服务器上，用 Docker Compose
启动后端、前端静态站点和 Caddy。输入域名时 Caddy 自动申请并续期 HTTPS
证书；没有域名时可以先运行 localhost HTTP 冒烟模式。

## 前提

- 一台 x86_64 或 ARM64 Linux 服务器；
- Docker Engine 与 Docker Compose v2（命令是 `docker compose`）已经安装；
- 域名模式下，域名的 A/AAAA 记录已指向服务器，入站 TCP 80/443 与 UDP
  443 已放行；
- 当前用户可以运行 `docker info`。

脚本会检查这些本机依赖，但不会远程安装 Docker、修改防火墙或 DNS。

## 一行启动

在仓库根目录运行：

```bash
bash deploy/deploy.sh patchproof.example.com
```

把示例域名替换成自己的域名，不要带协议、路径或端口。脚本会完成配置写入、
持久目录创建、镜像构建、服务启动和后端健康等待。Caddy 将站点地址作为唯一的
HTTPS 配置源，后端 CORS 同时由该地址派生，无需再手改 Compose 或 Caddyfile。

没有域名时，可在部署主机上启动 HTTP 冒烟模式：

```bash
bash deploy/deploy.sh --localhost
curl --fail http://localhost/api/health
```

`--localhost` 只面向主机本机访问，不会申请证书，也不应作为公网部署方式。

## 持久数据与任务仓库

首次启动会创建两个不会进入 Git 的目录：

- `deploy/data/`：SQLite 数据库、运行工作区和 receipt；
- `deploy/repositories/`：提供给 PatchProof 的任务仓库。

例如，在宿主机把仓库放到 `deploy/repositories/my-project`，Web UI 的仓库路径
填写 `/repositories/my-project`。该目录以读写方式挂载，因为人工批准 Apply 后
PatchProof 需要写回原仓库。升级和普通卸载都不会删除这两个目录，迁移或升级前
应备份它们。

部署脚本只生成站点地址和 CORS origin，不读取、接收或写入 LLM key。每位用户
仍在浏览器中提供自己的 provider 配置；不要给 Compose 增加共享的全局 API key。

## 日常运维

查看状态与容器健康状态：

```bash
bash deploy/deploy.sh status
```

查看后端和 Caddy 日志：

```bash
bash deploy/deploy.sh logs
```

升级时先获取已经审阅的代码，再幂等重建服务：

```bash
git pull --ff-only && bash deploy/deploy.sh upgrade
```

`upgrade` 复用 `deploy/.env` 中保存的域名/localhost 配置，并保留数据库、任务
仓库、Caddy 证书和其他命名卷。

移除容器和网络：

```bash
bash deploy/deploy.sh uninstall
```

这不会删除 `deploy/data/`、`deploy/repositories/` 或 Docker 命名卷，避免误删
数据库和证书。确认不再需要后，再由管理员自行备份并删除相应数据。

切换域名或从 localhost 上线时，直接重新运行域名形式的一行命令即可。脚本会
原子覆盖仅包含站点配置的 `deploy/.env`，然后幂等执行 `docker compose up`。

## 组成与健康检查

Compose 包含三个服务：

- `frontend-build`：按 lockfile 构建 Vite 静态资源并更新共享 webroot；
- `backend`：运行 Uvicorn，`/health` 通过后才允许 Caddy 启动；
- `caddy`：提供静态文件、SPA fallback、`/api/*` 反代与自动 HTTPS，并持续验证
  Caddy 配置。

若启动失败，脚本会显示 Compose 服务状态和下一步日志命令。常见原因是 Docker
daemon 权限、80/443 端口被占用、域名尚未解析到本机，或 ACME 无法从公网访问
80/443。

也可以直接检查：

```bash
docker compose --project-directory deploy --env-file deploy/.env \
  -f deploy/docker-compose.yml ps
docker compose --project-directory deploy --env-file deploy/.env \
  -f deploy/docker-compose.yml logs --tail=200 backend caddy
```

## Docker 评测边界

默认 Compose **不会**把宿主机 Docker socket 挂入后端，也不会在后端镜像内提供
Docker daemon。因此，服务器使用 Docker 来承载 PatchProof，不代表 Web 应用中的
`benchmark real`、公开语料解析或 `DockerEvalExecutor` 自动获得了 Docker 隔离能力；
这些评测在默认一键部署中应显示为 blocked/unavailable，且不会退回宿主进程冒充
隔离执行。

日常任务仍由后端容器内的 `ProcessExecutor` 运行。目标仓库若需要额外语言运行时、
系统库或测试工具，应审阅并定制 `deploy/backend.Dockerfile`。不要为了启用评测而
把 `/var/run/docker.sock` 直接挂入联网的共享后端；该 socket 等价于高权限宿主机
控制面，不属于本一键部署的安全边界。

## 上线检查

1. `bash deploy/deploy.sh status` 中后端为 `healthy`、Caddy 为运行状态；
2. `https://你的域名/api/health` 返回 `status: ok`；
3. 备份覆盖 `deploy/data/` 和 `deploy/repositories/`；
4. 只有可信用户可以访问站点。当前部署不自带登录/鉴权，公网共享前应在 Caddy
   前增加符合团队要求的访问控制。
