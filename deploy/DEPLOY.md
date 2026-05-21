# MySearch-Proxy 部署手顺（东京 LH · 43.165.170.157）

> 适用对象：单机 + 已存在 host-mode Caddy（容器名 `caddy`）+ certimate 手签证书的部署。
> 目标域名：`search.uctest.cn`（DNS 已指向 43.165.170.157）。
> 镜像源：`ghcr.io/chenp0401/mysearch-proxy:latest`（**Private**，需 PAT 登录）。

---

## 0. 拓扑

```
Internet ──► Caddy (host net, :80/:443)
                │   tls /etc/ssl/certimate/search.uctest.cn.{crt,key}
                ▼
            127.0.0.1:9874  ──►  mysearch-proxy 容器 (bridge, 仅绑回环)
                                    │
                                    ├── /app/data  -> ./data (SQLite)
                                    └── env_file: ./.env
```

服务器现状（已确认）：
- OpenCloudOS 9.4 / Docker 28.4 / Compose v2.30
- 已有容器：`caddy / searxng(8080) / new-api(3000) / new-api-mysql / new-api-redis / redis`
- 9874 端口空闲；内存 3.6Gi（compose 内已加 `mem_limit: 256m` 兜底）
- Caddyfile 路径：`/root/.openclaw/searxng/Caddyfile`

---

## 1. 一次性准备：GHCR 登录 + certimate 证书

### 1.1 在 GitHub 生成 PAT

进入 <https://github.com/settings/tokens> -> **Generate new token (classic)**：
- Scope 只勾 `read:packages`
- 生成后复制 token，下面记作 `<GHCR_PAT>`

### 1.2 服务器上登录 GHCR

```bash
ssh root@43.165.170.157
echo '<GHCR_PAT>' | docker login ghcr.io -u chenp0401 --password-stdin
# 应输出 Login Succeeded；凭证落在 /root/.docker/config.json
```

### 1.3 在 certimate 平台为 search.uctest.cn 签发证书

1. 登录 certimate 控制台 -> 新建工作流 -> 申请 `search.uctest.cn` 证书（DNS-01）。
2. 部署节点选「SSH 部署到本机文件」，目标路径：
   - 证书：`/etc/ssl/certimate/search.uctest.cn.crt`
   - 私钥：`/etc/ssl/certimate/search.uctest.cn.key`
3. 工作流跑成功后，在服务器上确认：
   ```bash
   ls -l /etc/ssl/certimate/search.uctest.cn.*
   # 应看到 .crt 和 .key 两个文件
   ```
4. （可选）打开 certimate 自动续期，到期前 30 天自动覆盖文件。

---

## 2. 服务器目录与配置

```bash
ssh root@43.165.170.157
mkdir -p /root/.openclaw/mysearch-proxy/data
# ★ 必须把 data 目录的 owner 改成 uid=999（容器内非 root 的 app 用户），
#   否则容器启动会因为 sqlite3.OperationalError: unable to open database file 而崩溃。
chown -R 999:999 /root/.openclaw/mysearch-proxy/data
cd /root/.openclaw/mysearch-proxy
```

把以下两个文件从仓库复制（或粘贴）过来：

### 2.1 docker-compose.yml

直接照搬仓库 `proxy/docker-compose.yml`：

```bash
curl -fsSL -H "Authorization: Bearer <GH_PAT_with_repo_read>" \
  https://raw.githubusercontent.com/chenp0401/MySearch-Proxy/main/proxy/docker-compose.yml \
  -o docker-compose.yml
```

> 仓库私有时需 PAT；如果你倾向手动粘贴，从本地仓库 `proxy/docker-compose.yml` 复制即可。

### 2.2 .env

```bash
# 从仓库模板生成
curl -fsSL -H "Authorization: Bearer <GH_PAT_with_repo_read>" \
  https://raw.githubusercontent.com/chenp0401/MySearch-Proxy/main/proxy/.env.production.example \
  -o .env
chmod 600 .env
nano .env   # 替换 ADMIN_PASSWORD 等占位符
```

关键字段（必改）：
- `ADMIN_PASSWORD=` 24 位随机串，建议 `openssl rand -base64 24` 生成
- 其余按需

> 通义 Qwen 的 API key **不在 .env 里**，部署起来后再到 `https://search.uctest.cn/admin` 控制台 -> Service: qwen -> 注入。

---

## 3. 启动 proxy

```bash
cd /root/.openclaw/mysearch-proxy
docker compose pull
docker compose up -d
docker compose ps           # STATE 应为 Up (healthy) 大约 30s 后
docker compose logs -f --tail=50    # 看到 Uvicorn running on 0.0.0.0:9874 即正常
```

冒烟测试（服务器内）：

```bash
curl -fsS http://127.0.0.1:9874/healthz
# 期望：{"status":"ok"...}
```

---

## 4. 接入 Caddy

### 4.1 把片段追加到 Caddyfile

```bash
cat /root/.openclaw/mysearch-proxy/Caddyfile.search.snippet >> /root/.openclaw/searxng/Caddyfile
```

`Caddyfile.search.snippet` 来源：仓库 `deploy/Caddyfile.search.snippet`，先 scp / curl 上来。

### 4.2 校验语法 + reload

```bash
docker exec caddy caddy validate --config /etc/caddy/Caddyfile
docker exec caddy caddy reload   --config /etc/caddy/Caddyfile
```

### 4.3 端到端验证

在你本地（不在服务器上）执行：

```bash
curl -I https://search.uctest.cn/healthz
# 期望：HTTP/2 200，证书 CN 为 search.uctest.cn
```

浏览器打开 <https://search.uctest.cn/admin>，用 `ADMIN_PASSWORD` 登录控制台。

---

## 5. 注入 API key

控制台 -> 选服务（tavily / firecrawl / exa / qwen）-> 「添加 key」：

- **tavily**：`tvly-XXXXXXXX...`
- **qwen**（DashScope）：`sk-XXXXXXXX...`，至少 32 位
- **firecrawl / exa**：略

注入后控制台会显示状态与配额（qwen 卡片显示「实时额度暂时无法查询」是正常的，DashScope 没开放 usage 接口）。

---

## 6. 升级流程（CI 推完新镜像后）

```bash
ssh root@43.165.170.157
cd /root/.openclaw/mysearch-proxy
docker compose pull
docker compose up -d        # 老容器自动替换为新镜像
docker compose ps
docker image prune -f       # 清理悬空镜像
```

GitHub 主分支 push 后，GitHub Actions 会自动构建并推 `:latest` + `:sha-xxxxxxx` + `:main`。
打 tag `vX.Y.Z` 时也会推 `:vX.Y.Z`，便于回滚。

回滚到指定 sha：

```bash
docker pull ghcr.io/chenp0401/mysearch-proxy:sha-abcdef0
# 把 docker-compose.yml 里 image: 改成同一个 tag，再 docker compose up -d
```

---

## 7. 常见排障

| 现象 | 排查 |
| --- | --- |
| `docker compose pull` 提示 `denied` | GHCR 登录失效；重跑 `docker login ghcr.io -u chenp0401` |
| Caddy `reload` 报 tls 文件不存在 | certimate 证书未部署到 `/etc/ssl/certimate/`；先在 certimate 工作流里跑一次部署 |
| `https://search.uctest.cn` 502 | proxy 容器没起，或没绑 127.0.0.1:9874。`docker compose logs` 看 Uvicorn 是否启动 |
| 容器一直 `Restarting`，logs 报 `sqlite3.OperationalError: unable to open database file` | 宿主 `data/` 目录属主不是 999:999，容器内非 root 用户写不进去。`chown -R 999:999 /root/.openclaw/mysearch-proxy/data` 后 `docker compose up -d --force-recreate` |
| `https://search.uctest.cn` 证书是 ZeroSSL/LE | Caddyfile 没写 `tls ...`，落到了 Caddy 自动 ACME。检查片段是否真的追加生效 |
| 内存压力大 | `docker stats mysearch-proxy`；compose 已设 `mem_limit: 256m`，超限会被 OOM kill 然后 unless-stopped 重启 |
| 想看真实客户端 IP | proxy 已通过 `X-Forwarded-For / X-Real-IP` 收到；Caddy 全局 `trusted_proxies` 已配 Cloudflare 段，如果直连 LH 就是 client 真实 IP |

---

## 8. 文件清单（仓库内）

| 路径 | 说明 |
| --- | --- |
| `.github/workflows/docker-publish.yml` | GHCR 镜像 CI |
| `proxy/Dockerfile` | 镜像构建（python:3.12-slim, 非 root, healthcheck） |
| `proxy/docker-compose.yml` | 服务器端 pull 模式（127.0.0.1:9874, mem_limit 256m） |
| `proxy/.env.production.example` | 生产 .env 模板 |
| `deploy/Caddyfile.search.snippet` | Caddy 反代片段（certimate 手签） |
| `deploy/DEPLOY.md` | 本文件 |
