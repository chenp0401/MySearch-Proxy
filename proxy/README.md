# MySearch Proxy Console

[English Guide](./README_EN.md) · [返回仓库](../README.md)

`proxy/` 是 MySearch 的控制台与代理层。

它不是单纯的 key 面板，而是整套 `proxy-first` 架构的中间层：

- 上游连 Tavily / Firecrawl / Exa / 可选 Social
- 下游给 MySearch MCP、OpenClaw skill 和其他 Agent 发统一 token
- 页面里同时看 key 池、token 池、调用统计和额度信息

![MySearch Console Hero](../docs/images/mysearch-console-hero.jpg)

## 它解决什么问题

如果没有 Proxy，常见问题会很散：

- 每台客户端都要单独填 provider key
- OpenClaw 和本地 Codex 的配置容易分叉
- token、额度、调用统计没有统一入口
- 上游 provider 一旦换地址，所有客户端都要跟着改
- Social / X 这条链很难跟 Web / Docs 搜索放在一个控制平面里

`MySearch Proxy` 的目标就是把这些收回来。

## 支持的能力

### Tavily

代理入口：

- `POST /api/search`
- `POST /api/extract`
- `POST /search`、`POST /extract` —— **官方风格透明端点**，用于让官方 `tavily-python` SDK / Agent Skill 直接接入（详见下文「官方 SDK 透明接入」）

控制台能力：

- key 池
- token 池
- 使用量同步
- 调用统计

### Firecrawl

代理入口：

- `POST /firecrawl/v2/search`
- `POST /firecrawl/v2/scrape`
- `ANY /v2/{path}` —— **官方风格透明端点**，路径与官方 `https://api.firecrawl.dev/v2/*` 完全一致

控制台能力：

- key 池
- token 池
- credits 同步
- 调用统计

### Exa

代理入口：

- `POST /exa/search`

控制台能力：

- key 池
- token 池
- 调用统计

说明：

- Exa 当前在控制台里支持接入和分发
- 实时官方额度暂时无法查询，所以页面会明确标注这一点

### MySearch 通用 token

控制台能力：

- 创建 `mysp-` 开头的 MySearch token
- 一次接通 Tavily / Firecrawl / Exa
- 给 `mysearch/.env` 和 OpenClaw skill 直接复用
- 记录这类 token 的调用统计

当前策略：

- 默认关闭 token 小时 / 日 / 月限流
- token 只做鉴权与统计，不做配额拦截

### Social / X

代理入口：

- `GET /social/health`
- `POST /social/search`

控制台能力：

- 上游 base URL 管理
- gateway token 管理
- 兼容 admin API 对接
- token 状态展示

## 官方 SDK / Agent Skill 透明接入

目标：**官方 SDK 或官方 Agent Skill（[Tavily Skills](https://docs.tavily.com/documentation/agent-skills)、[Firecrawl Skills](https://github.com/firecrawl/skills)）代码一行不改**，仅通过环境变量即可让其走 MySearch Proxy。

### 它是怎么实现的

1. **服务端镜像路由**（已内置）：MySearch 同时暴露官方风格的路径
   - Tavily：`POST /search`、`POST /extract`，与官方 `https://api.tavily.com/{search,extract}` 同构
   - Firecrawl：`ANY /v2/{path}`，与官方 `https://api.firecrawl.dev/v2/*` 同构
   - 鉴权同时支持 `Authorization: Bearer <token>`、`x-api-key` header、body `api_key` 字段，三选一

2. **SDK 透明补丁**（[`tavily_patch.py`](./tavily_patch.py)，可选）：因为官方 SDK 写死了上游域名，补丁通过 monkey-patch 让 SDK 在未显式指定 base URL 时自动从环境变量读取，实现真正的 0 修改接入。

### 三步接入

**Step 1**：在 MySearch 控制台创建一个 `mysp-` 通用 token（`/admin` → token 池 → 新建）。

**Step 2**：设置环境变量：

```bash
export TAVILY_BASE_URL=https://search.uctest.cn
export TAVILY_API_KEY=mysp-xxxxxxxxxxxxxxxxxxxxxxxx

export FIRECRAWL_API_URL=https://search.uctest.cn
export FIRECRAWL_API_KEY=mysp-xxxxxxxxxxxxxxxxxxxxxxxx
```

**Step 3**：在你的 Skill / 应用入口最前面加一行（仅一次）：

```python
import tavily_patch  # noqa: F401
```

之后官方 Skill 里的代码（`TavilyClient(api_key=...)`、`FirecrawlApp(api_key=...)`）**完全不需要改动**，所有调用会自动走 MySearch Proxy。

### 不想用 patch 的写法（也可以）

两个官方 SDK 其实本身都接受显式的 base URL 参数，如果你能改一行 Skill 初始化代码，可以不用 patch：

```python
from tavily import TavilyClient
client = TavilyClient(
    api_key="mysp-xxxx",
    api_base_url="https://search.uctest.cn",
)

from firecrawl import FirecrawlApp
app = FirecrawlApp(
    api_key="mysp-xxxx",
    api_url="https://search.uctest.cn",
)
```

### Qwen WebSearch MCP（原生 MCP 客户端零改造接入）

针对**只认 streamable-http MCP 协议**的客户端（Claude Code CLI、Cursor、各类 MCP host），MySearch 暴露了一个标准 MCP 网关：

```
POST https://<your-proxy>/qwen/mcp
Authorization: Bearer mysp-xxxxxxxxxxxxxxxxxxxxxxxx
```

它做了这些事：

- `initialize` / `notifications/initialized` / `tools/list` 在网关本地直接应答，不消耗 Qwen 配额；
- `tools/call(bailian_web_search)` 由网关从 Qwen key 池里挑一把可用 key，转发到阿里云百炼官方 `https://dashscope.aliyuncs.com/api/v1/mcps/WebSearch/mcp`，把搜索结果原样回 JSON-RPC；
- 全程**只看 mysp- token**，不向客户端暴露任何上游真 key；调用成功 / 失败都会进 `pool.report_result` + 控制台 token 用量统计（`service=qwen, endpoint=mcp`）。

#### Claude Code CLI 接入

```bash
claude mcp add --transport http mysearch-qwen \
  https://search.uctest.cn/qwen/mcp \
  --header "Authorization: Bearer mysp-xxxxxxxxxxxxxxxxxxxxxxxx"

claude mcp list
# mysearch-qwen: https://search.uctest.cn/qwen/mcp (HTTP) - ✓ Connected
```

之后在 Claude Code 里直接说「帮我搜一下 …」即可，模型会自动调用 `bailian_web_search` 工具。

#### Cursor / 通用 MCP 客户端

`mcp.json`（或对应配置文件）：

```json
{
  "mcpServers": {
    "mysearch-qwen": {
      "type": "http",
      "url": "https://search.uctest.cn/qwen/mcp",
      "headers": {
        "Authorization": "Bearer mysp-xxxxxxxxxxxxxxxxxxxxxxxx"
      }
    }
  }
}
```

#### 手动 smoke test（不依赖任何客户端）

```bash
URL=https://search.uctest.cn/qwen/mcp
TOKEN=mysp-xxxxxxxxxxxxxxxxxxxxxxxx

# 1) initialize
curl -sS -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json,text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"smoke","version":"1.0"}}}'

# 2) tools/list
curl -sS -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json,text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

# 3) tools/call
curl -sS -X POST "$URL" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json,text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"bailian_web_search","arguments":{"query":"MCP protocol 2025","count":3}}}'
```

> 与 Tavily/Firecrawl 的"官方风格透明路由"不同，这里走的是**标准 MCP JSON-RPC**：客户端不需要任何 SDK monkey-patch，只要支持 streamable-http transport 就能直接连。

## 当前推荐用法

推荐你把它当成统一入口，而不是单独使用某一个 provider 工作台。

标准链路：

```text
上游 provider
  -> MySearch Proxy
     -> 生成 mysp- token
        -> MySearch MCP / OpenClaw skill / 其他 Agent
```

客户端只需要：

```env
MYSEARCH_PROXY_BASE_URL=https://your-mysearch-proxy.example.com
MYSEARCH_PROXY_API_KEY=mysp-...
```

## 控制台刷新性能（已优化）

为避免页面每次刷新都被远程额度同步拖慢，控制台现在默认采用：

- `/api/stats` 快速返回（短缓存）
- 额度同步改为手动触发（或后台节流同步）
- 写操作后前端会强制刷新，避免读到旧缓存

关键环境变量：

```env
STATS_CACHE_TTL_SECONDS=8
DASHBOARD_AUTO_SYNC_ON_STATS=0
DASHBOARD_BACKGROUND_SYNC_ON_STATS=1
DASHBOARD_BACKGROUND_SYNC_MIN_INTERVAL_SECONDS=45
```

说明：

- 如果你更看重“每次刷新都立刻拉最新额度”，可设 `DASHBOARD_AUTO_SYNC_ON_STATS=1`。
- 默认推荐保持 `0`，然后在页面点击“同步额度”按钮做显式刷新。

## 部署

### 方式 A：直接跑 Docker Hub 镜像

```bash
mkdir -p mysearch-proxy-data

docker run -d \
  --name mysearch-proxy \
  --restart unless-stopped \
  -p 9874:9874 \
  -e ADMIN_PASSWORD=change-me \
  -v $(pwd)/mysearch-proxy-data:/app/data \
  skernelx/mysearch-proxy:latest
```

访问：

```text
http://localhost:9874
```

### 方式 B：docker compose

```bash
cd proxy
docker compose up -d
```

### 方式 C：本地源码运行

```bash
cd proxy
pip install -r requirements.txt
ADMIN_PASSWORD=change-me uvicorn server:app --host 0.0.0.0 --port 9874
```

## 首次初始化建议

第一次打开页面后，按这个顺序做最稳：

1. 用 `ADMIN_PASSWORD` 登录控制台
2. 添加 Tavily / Firecrawl / Exa 的上游 key
3. 如果你要 Social / X，再补它的 upstream 配置
4. 执行一轮 usage sync
5. 创建 MySearch 通用 token
6. 把 `MYSEARCH_PROXY_BASE_URL` 和 `MYSEARCH_PROXY_API_KEY` 填给客户端

当前控制台已经带密码登录，不再适合匿名裸放在公网。

## 下游怎么接

### 给 `mysearch/` MCP

```env
MYSEARCH_PROXY_BASE_URL=https://your-mysearch-proxy.example.com
MYSEARCH_PROXY_API_KEY=mysp-...
```

### 给 OpenClaw skill

```json
{
  "skills": {
    "entries": {
      "mysearch": {
        "enabled": true,
        "env": {
          "MYSEARCH_PROXY_BASE_URL": "https://your-mysearch-proxy.example.com",
          "MYSEARCH_PROXY_API_KEY": "mysp-..."
        }
      }
    }
  }
}
```

## 页面与数据

控制台页面会按服务拆成独立区域：

- Tavily
- Exa
- Firecrawl
- Social / X
- MySearch 通用 token

这样做的目的是：

- 各服务额度不会混在一起
- token 不会串用
- 调用统计更清楚
- 下游接线一眼能看懂

界面预览：

![MySearch Console Workspaces](../docs/images/mysearch-console-workspaces.jpg)

默认数据目录：

- Docker compose
  - `./data`
- `docker run` 示例
  - `$(pwd)/mysearch-proxy-data`

## 认证与安全

关键环境变量：

```env
ADMIN_PASSWORD=change-me
ADMIN_SESSION_COOKIE=mysearch_proxy_session
ADMIN_SESSION_MAX_AGE=2592000
```

建议：

- 第一时间改掉默认管理员密码
- 放公网时务必配 HTTPS 反代
- 不要把生产上游 key 暴露到前端代码仓库
- 只把 `mysp-` token 发给下游客户端

## 支持的 API

管理和面板相关：

- `GET /`
- `GET /api/session`
- `POST /api/session/login`
- `POST /api/session/logout`
- `GET /api/stats`
- `GET /api/settings`
- `PUT /api/settings/social`
- `GET /api/keys`
- `POST /api/keys`
- `GET /api/tokens`
- `POST /api/tokens`
- `POST /api/usage/sync`

搜索代理相关：

- `POST /api/search`
- `POST /api/extract`
- `POST /firecrawl/v2/search`
- `POST /firecrawl/v2/scrape`
- `POST /exa/search`
- `POST /qwen/web_search` —— Qwen WebSearch REST 接口
- `POST /qwen/mcp` —— Qwen WebSearch 标准 MCP（streamable-http）网关，给 Claude Code / Cursor 等原生 MCP 客户端用
- `GET /social/health`
- `POST /social/search`

## 什么时候看别的文档

- 你要安装 MCP：
  看 [../mysearch/README.md](../mysearch/README.md)
- 你要给 AI 安装 skill：
  看 [../skill/README.md](../skill/README.md)
- 你要装 OpenClaw bundle：
  看 [../openclaw/README.md](../openclaw/README.md)
