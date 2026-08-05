# PatchProof 免费部署规划（用户自带 API key）

目标：**免费**把 PatchProof 部署成一套「大家都能用」的系统，每位用户**自带自己的 LLM API key**（base_url / model / api_key 自己填），不共享你的密钥。

---

## 0. 一个决定性的前提

| 路径 | 执行器 | 需要 Docker? |
|---|---|---|
| 日常任务（前端建任务 → 修仓库 → receipt） | `ProcessExecutor`（本地子进程，隔离靠仓库快照副本） | ❌ 不需要 |
| 评测（`benchmark real` / `resolve-public` 探针） | `DockerEvalExecutor`（钉死 digest 容器） | ✅ 需要 Docker daemon |

所以：

- **只跑日常任务** → 任何能跑 Python 3.12 的平台都行，甚至全免费 PaaS。
- **要跑官方评测** → 必须有一台带 Docker daemon 的主机（虚拟化 OK）。

## 1. 部署架构

```
[用户浏览器] ──HTTPS──► [Caddy / nginx]（自动证书 + 静态前端 + /api 反代）
                           │  /                    │  /api/* → 去掉前缀
                           ▼                       ▼
                    前端静态文件                [uvicorn :8010] patchproof.api
                    (frontend/dist)                │
                                                  ├─ data/patchproof.db  (SQLite 持久卷)
                                                  ├─ data/runs/          (工作区快照 / receipt)
                                                  └─（可选）Docker daemon ── 仅评测需要
```

前端用 Vite 构建，产物是纯静态文件；`/api` 前缀在反代层剥掉并转发给 8010。

## 2. 自带 API key（已实现）

前端「LLM Provider」折叠面板可填 `base_url` / `model` / `api_key` / `transport`，存 `localStorage`，随 `POST /tasks` 提交。

- 后端 `POST /tasks` 接受可选 `provider`；`TaskRecord` 内存持有完整 provider（含 key）。
- `runner` 每次 `run()` 用显式构造的 `Settings(anthropic_api_key=..., ...)` 构建独立 `LLMClient`（共享 `AgentRunner` 不做全局覆盖，避免并发竞态）。
- **安全约束（已生效）**：
  - api_key **不落 SQLite**：持久化的是 `provider_view()`（key 替换为 `***configured***`）。
  - key **不进日志 / 响应 / 快照**：`TaskSnapshot.provider` 与 API 响应都是脱敏视图。
  - 前端 `localStorage` 存 key 有 XSS 风险——README 已知局限里注明。
  - 共享部署默认不设任何 `PATCHPROOF_ANTHROPIC_API_KEY` 全局密钥，否则会盖掉用户自带 key（`choose()` 优先读 `PATCHPROOF_*`）。

## 3. 免费部署方案对比

| 方案 | 免费额度 | Docker 评测 | 日常任务 | 休眠/持久化 | 适合 |
|---|---|---|---|---|---|
| **A. Oracle Cloud 免费 VM** ⭐ | 永久免费：ARM Ampere 4 OCPU / 24GB / 200GB | ✅ | ✅ | 无休眠；持久磁盘 | 完整功能、长期稳定 |
| **B. 便宜 VPS**（阿里/腾讯轻量） | ¥30–50/月 | ✅ | ✅ | 无休眠 | 国内访问快、图省事 |
| **C. 全免费 PaaS**（Render/Railway + Vercel/CF Pages） | 各平台免费 tier | ❌ | ✅（ProcessExecutor） | Render 免费 tier 15min 休眠、磁盘非持久 | 想 0 成本体验，可接受功能打折 |

**结论**：推荐 **方案 A**（永久免费 + 完整功能）。想 0 成本先跑通体验再上真机，用 **方案 C**。

## 4. 方案 A：Oracle Cloud 免费 VM + Docker Compose + Caddy

### 4.1 申请与初始化
1. 注册 Oracle Cloud 免费账号，创建 **VM.Standard.A1.Flex**（ARM，4 OCPU / 24GB，Ubuntu 24.04）。
2. 安全组放行 `80 / 443 / 22`。
3. SSH 登录后装 Docker：

```bash
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER   # 重登生效
```

### 4.2 目录结构
```
/opt/patchproof/
├─ docker-compose.yml
├─ Caddyfile
├─ backend/            # git clone 你的 patchproof 仓库（含 src/, pyproject.toml）
│  └─ data/            # 持久卷 ← 映射到宿主 /opt/patchproof/data
└─ frontend-dist/      # 前端构建产物
```

### 4.3 构建前端
```bash
cd frontend && pnpm install && pnpm build     # 产物在 frontend/dist/
cp -r frontend/dist /opt/patchproof/frontend-dist/
```

### 4.4 `docker-compose.yml`

```yaml
services:
  backend:
    build:
      context: ./backend
      dockerfile: deploy/Dockerfile        # 见 4.5
    ports: ["127.0.0.1:8010:8010"]
    volumes:
      - ./data:/app/data
    environment:
      PATCHPROOF_DATABASE_PATH: /app/data/patchproof.db
      PATCHPROOF_CORS_ORIGINS: "https://patchproof.example.com"
      # 不设任何全局 API key —— 用户自带（见第 2 节）
    restart: unless-stopped

  # 可选：评测需要 Docker daemon（只读 socket，严格风险提示，见第 7 节）
  # 若不需要评测可整段删掉

  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - ./frontend-dist:/srv/www:ro
    restart: unless-stopped

volumes:
  caddy_data:
```

### 4.5 后端镜像 `deploy/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN pip install --no-cache-dir uv && uv sync --frozen --no-dev
CMD ["uvicorn", "patchproof.api:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8010"]
```

> Linux 容器内不需要 Windows Proactor 策略；也不需要 `--reload`。

### 4.6 `Caddyfile`（自动 HTTPS）

```
patchproof.example.com {
    root * /srv/www
    file_server
    handle /api/* {
        uri strip_prefix /api
        reverse_proxy 127.0.0.1:8010
    }
}
```

> 域名若还没有：先用 `duckdns.org`（免费动态域名）或直接 Cloudflare 托管的域名。

### 4.7 启动
```bash
cd /opt/patchproof && docker compose up -d --build
```

## 5. 方案 C：全免费 PaaS（无 Docker 评测）

### 5.1 后端（Render / Railway）
1. 后端仓库推送 GitHub，在 Render 建 **Web Service**：
   - Build: `pip install uv && uv sync --frozen --no-dev`
   - Start: `uvicorn patchproof.api:app --app-dir src --host 0.0.0.0 --port $PORT`
   - 环境变量：`PATCHPROOF_DATABASE_PATH` 指向持久磁盘（Railway Volume；Render 免费 tier 磁盘重启会重置，可接受则用，或接外部 SQLite/S3 快照）。
   - 免费 tier 注意：Render 15 分钟无流量休眠，首次请求冷启动。
2. 暴露的 URL 记下来，如 `https://patchproof-backend.onrender.com`。

### 5.2 前端（Vercel / Cloudflare Pages）
1. `frontend` 推 GitHub，建静态站点：build command `pnpm build`，output `dist`。
2. 配置 rewrite：`/api/*` → `https://patchproof-backend.onrender.com/*`（Vercel `vercel.json` 或 CF Pages redirects）。

### 5.3 局限
- 无 Docker → 不能跑 `benchmark real` 评测；日常任务可用。
- SQLite 持久化是最大坑：确保卷/外部存储，否则用户任务记录随重启丢失。

## 6. 多用户与鉴权（按需）

| 级别 | 做法 | 成本 |
|---|---|---|
| 最简（内网/小团队） | 不鉴权，大家自带 key | 0 |
| 基础 | Caddy `forward_auth` 或 Basic Auth 挡整个站点 | 0 |
| 正式 | 前端登录 + 后端 token；每用户 provider 配置存用户表（key 仍由用户本地填） | 开发量中 |

## 7. 运维与安全清单

- **备份**：`data/patchproof.db` + `data/runs/` 定时打包（cron / restic）。
- **评测用 Docker socket**：如果开启评测容器，`--network none` + 只读 rootfs 已是内置默认；宿主机 docker.sock **不要**以可写方式挂进后端（风险极高），只允许后端通过 HTTP 受控拉起评测容器。
- **密钥**：全局不设 `PATCHPROOF_ANTHROPIC_API_KEY`；用户 key 仅内存持有。
- **CORS**：`PATCHPROOF_CORS_ORIGINS` 只填你的域名。
- **资源**：Compose 里给后端 `mem_limit`；`data/runs/` 定期清理过期工作区（快照可能占用磁盘）。
- **升级**：git pull 后端 + `docker compose up -d --build`，前端重新 build 替换 `frontend-dist/`。

## 8. 上线前清单

部署构件（`deploy/backend.Dockerfile`、`frontend.Dockerfile`、`docker-compose.yml`、`Caddyfile`、`.dockerignore`）与「自带 API key」已随仓库提交。上线前只需：

1. 把 `docker-compose.yml` 里的 `YOUR_DOMAIN`（Caddyfile 也是）换成你的真实域名。
2. 确认 `frontend/dist` 由 `frontend-build` 服务自动构建（首次 `docker compose up --build`）。
3. 远端 git 仓库支持（方式 B）尚未实现——v1 让用户填服务器上已 clone 的本地路径即可。
