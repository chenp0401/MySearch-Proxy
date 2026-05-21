# 需求文档 · 通义（Qwen / DashScope）Provider 接入

> 项目：`MySearch-Proxy`（私有 fork，本地路径 `/Users/chenp/dev/MySearch-Proxy`）
> 决策基线：HTML 评估文档「走向 B 完整版」+ 用户 4 项设计决策（Q1=a，Q2=proxy 智能网关，Q3=复用 answer 并去重，Q4=私有 fork）
> 上游基准版本：`52df2a0` (v0.1.13, `Harden error handling and bump to 0.1.13`)
> 制定日期：2026-05-21

---

## 引言

为了让东京 LH 上的统一搜索网关能长期消化 1 把通义 DashScope 的 web search key，并在 2 把 Tavily key 月配额耗尽时自动接管中文相关查询，本需求文档定义在 `MySearch-Proxy` 私有 fork 中新增第 4 个 provider —— **qwen**。

核心设计原则：

1. **能力定位单一**：qwen 仅承担 web 搜索通道，不替代 Firecrawl（docs / extract）也不替代 xAI（social）。
2. **路由智能化下沉到 proxy**：mysearch 客户端层调用 `/api/search` 不感知 qwen，proxy 在 Tavily key pool 全空时内部转发到 qwen 后端，并在响应头中标记 `X-Provider: qwen`，保证调用方零改造。
3. **响应归一化复用 Tavily schema**：通义返回的 `text`（模型摘要）映射到 MySearch schema 的 `answer` 字段；`search_results` 映射到 `results`；与 Tavily 答案做去重，避免下游 agent 看到重复信息。
4. **私有 fork 友好**：所有改动尽量集中在新建模块（`proxy/providers/qwen.py`、`mysearch/providers/qwen.py`），最小化对 `clients.py` 主干的侵入，便于未来 rebase 上游 0.1.x 时合并冲突最少。
5. **额度跟踪降级**：通义无官方 usage 查询接口，照搬 Exa 的 `supported=False` 模式，控制台显示「不支持自动同步」。

---

## 需求

### 需求 1 · Provider 元数据与配置

**用户故事：** 作为运维方，我希望能通过环境变量与控制台两种方式注入通义 API key，以便部署灵活、且不破坏现有 Tavily/Firecrawl/Exa 配置。

#### 验收标准

1. WHEN 系统启动 THEN `mysearch/config.py` SHALL 在 `MySearchConfig` dataclass 中新增 `qwen: ProviderConfig` 字段，默认 `base_url=https://dashscope.aliyuncs.com`，`auth_mode=bearer`，`auth_header=Authorization`，`auth_scheme=Bearer`。
2. WHEN 环境变量 `MYSEARCH_QWEN_API_KEY` 或 `MYSEARCH_QWEN_API_KEYS`（逗号分隔）被设置 THEN 配置加载 SHALL 将其填入 `qwen.api_keys`，多 key 支持轮询。
3. WHEN 环境变量 `MYSEARCH_PROXY_BASE_URL` 已设置但未单独提供 `MYSEARCH_QWEN_BASE_URL` THEN qwen 的 `base_url` SHALL 自动指向 proxy（与 tavily/firecrawl/exa 行为一致），`search` 路径默认为 `/qwen/search`。
4. WHEN `mysearch/.env.example` 被读取 THEN 文件 SHALL 在 Exa 段之后、xAI 段之前包含完整的 `MYSEARCH_QWEN_*` 配置示例段。
5. WHEN `ProviderName` Literal 类型被引用 THEN [clients.py](/Users/chenp/dev/MySearch-Proxy/mysearch/clients.py) 中的定义 SHALL 扩展为 `Literal["auto", "tavily", "firecrawl", "exa", "xai", "qwen"]`。

---

### 需求 2 · MySearch 客户端层显式调用支持

**用户故事：** 作为 MySearch 客户端的调用方（OpenClaw / Claude Code），我希望能通过 `provider="qwen"` 显式调用通义搜索，以便在中文场景下做对比测评。

#### 验收标准

1. WHEN `MySearchClient.search()` 被调用且 `provider="qwen"` THEN 客户端 SHALL 走新增的 `_search_qwen()` 分支，直连 DashScope 或经 proxy `/qwen/search`。
2. WHEN `_search_qwen()` 收到通义响应 THEN 它 SHALL 调用新增的 `_normalize_qwen_results()` 函数将通义原生字段（`output.text`、`output.search_results`）转换成 MySearch 标准 schema：`{answer, results: [{title, url, content, score}], citations, provider: "qwen"}`。
3. WHEN 通义返回成功但 `search_results` 为空 THEN `_normalize_qwen_results()` SHALL 返回 `results=[]` 而非抛异常，`answer` 字段仍透传 `text`。
4. WHEN 通义返回 HTTP 401/403 THEN 客户端 SHALL 抛出 `MySearchHTTPError(provider="qwen", is_auth_error=True)`，与现有 provider 行为一致。
5. WHEN `MySearchClient.health()` 被调用 THEN 返回 dict 的 `providers` 子项 SHALL 包含 `"qwen"` 键，结构与 `tavily`/`firecrawl` 等同（含 `configured` / `api_keys_count` / `keys_file` 等字段）。
6. WHEN `provider="auto"` 路由决策被执行 THEN 当前版本 SHALL **不**自动选中 qwen（保持 web→tavily、docs→firecrawl 默认行为不变），qwen 仅通过显式 `provider="qwen"` 触发 —— 自动 fallback 由 proxy 层负责（见需求 4）。

---

### 需求 3 · Proxy 数据库与 key pool 支持

**用户故事：** 作为运维方，我希望通义 key 能与 Tavily / Firecrawl / Exa 一样存储在 SQLite 中，并能通过控制台增删启停。

#### 验收标准

1. WHEN [database.py](/Users/chenp/dev/MySearch-Proxy/proxy/database.py) 被加载 THEN `SUPPORTED_SERVICES` SHALL 扩展为 `("tavily", "firecrawl", "exa", "qwen")`。
2. WHEN 创建通义 token THEN `TOKEN_PREFIX["qwen"]` SHALL 为 `"qwen-"`。
3. WHEN 通过控制台批量导入通义 key 文本 THEN `KEY_PATTERNS["qwen"]` SHALL 使用正则 `r"(sk-[A-Za-z0-9]{32,})"` 识别 DashScope key 格式。
4. WHEN `db.init_db()` 在已存在 v0.1.13 数据库上启动 THEN 现有的 `api_keys` / `tokens` / `usage_logs` 表 SHALL 无需 schema 迁移即可承载 `service="qwen"` 行（既有 `_ensure_service_columns` 保证兼容）。
5. WHEN [key_pool.py](/Users/chenp/dev/MySearch-Proxy/proxy/key_pool.py) 中的 `ServiceKeyPool` 被实例化 THEN 它 SHALL 自动包含 qwen 池（无需修改 `key_pool.py`，因其循环 `SUPPORTED_SERVICES`）。

---

### 需求 4 · Proxy 智能网关：Tavily → Qwen 自动 Fallback

**用户故事：** 作为 MySearch 客户端的调用方，我希望调用 `/api/search` 时不需要感知 provider，proxy 在 Tavily 配额耗尽时自动切换到通义。

#### 验收标准

1. WHEN 客户端 `POST /api/search` 时 `pool.get_next_key("tavily")` 返回 `None` THEN proxy SHALL **不**再返回 HTTP 503，而是尝试调用内部 qwen fallback 流程。
2. WHEN qwen fallback 被触发 AND `pool.get_next_key("qwen")` 返回有效 key THEN proxy SHALL 将原始 Tavily 请求体（`query` 等）经新增的 `translate_tavily_to_qwen_request()` 函数转换为 DashScope 请求体，转发到通义。
3. WHEN qwen fallback 成功返回 THEN proxy SHALL 使用新增的 `translate_qwen_to_tavily_response()` 函数把通义响应翻译回 Tavily 兼容的 JSON schema（含 `answer`、`results[].url/title/content`、`response_time` 等字段），保证调用方无感知。
4. WHEN qwen fallback 路径返回响应 THEN HTTP 响应头 SHALL 包含 `X-Provider: qwen`（正常 Tavily 路径包含 `X-Provider: tavily` 或不含）。
5. WHEN qwen fallback 也无可用 key（`get_next_key("qwen")` 也为 `None`）THEN proxy SHALL 返回 HTTP 503 且响应体 `detail` 文本明确说明「all providers exhausted」。
6. WHEN qwen fallback 上游返回 4xx/5xx THEN proxy SHALL 调用 `pool.report_result("qwen", ..., False)` 标记失败，**不**回退继续尝试 tavily（因 tavily 已确认无 key），直接将 502 抛回客户端。
7. WHEN 任何 fallback 调用发生 THEN proxy SHALL 在 `usage_logs` 表中记录一行 `service="qwen"`，`endpoint="search"`，便于后续审计 fallback 频次。
8. IF `/api/extract` 路径触发了空 tavily 池 THEN proxy SHALL **不**触发 qwen fallback（通义无 extract 能力），仍返回 503，文案明确「extract not supported by fallback providers」。

---

### 需求 5 · Proxy 显式 Qwen 端点

**用户故事：** 作为开发者，我希望通过 `/qwen/search` 直接调用通义而不经过 Tavily 入口，便于在 MySearch 客户端通过 `provider="qwen"` 显式选用，以及做端到端测试。

#### 验收标准

1. WHEN 新增路由 `POST /qwen/search` 被调用 THEN proxy SHALL 仿照 `proxy_exa_search`（[server.py:1724](/Users/chenp/dev/MySearch-Proxy/proxy/server.py)）的实现模式：取 token、取 key、转发、记录用量。
2. WHEN `/qwen/search` 转发请求到 DashScope THEN proxy SHALL 使用 `Authorization: Bearer {key}` 注入鉴权，**不**在请求体中泄露 key。
3. WHEN `/qwen/search` 收到响应 THEN proxy SHALL 透传 DashScope 原始 JSON（不做翻译），由调用方（mysearch 客户端）的 `_normalize_qwen_results` 自己处理。
4. WHEN `/qwen/search` 调用方未提供合法 token THEN proxy SHALL 返回 401，与现有 Exa/Firecrawl 路径行为一致。

---

### 需求 6 · 模型摘要复用与去重（Q3）

**用户故事：** 作为下游 agent，我不希望同时收到 Tavily 的 `answer` 和通义的 `text`，造成重复或冲突信息。

#### 验收标准

1. WHEN proxy 在需求 4 路径下把通义响应翻译回 Tavily schema THEN `translate_qwen_to_tavily_response()` SHALL 把通义的 `output.text` 写入 `answer` 字段。
2. WHEN MySearch 客户端在 `verify` / `deep` 模式下同时拿到 Tavily 和通义答案（注：当前版本 fallback 是互斥的，不会并发，但 `cross-provider verify` 路径可能并发）THEN 客户端 SHALL 在新增的 `_dedupe_answers()` 中执行去重：若两个 answer 文本归一化后（去空白、小写）Jaccard 相似度 ≥ 0.6 则只保留 Tavily 的；否则两者拼接为 `"{tavily_answer}\n\n[通义补充]: {qwen_answer}"`。
3. WHEN 通义 `output.text` 为空字符串或仅含 `"很抱歉"`/`"无法回答"` 类拒答 THEN 翻译函数 SHALL 将 `answer` 设为空字符串，避免污染下游。

---

### 需求 7 · 控制台 UI 与额度同步降级

**用户故事：** 作为运维方，我希望在控制台能看到通义 service 卡片，能管理 key，但能容忍其无实时额度。

#### 验收标准

1. WHEN 用户访问 proxy 控制台首页 THEN [console.html](/Users/chenp/dev/MySearch-Proxy/proxy/templates/console.html) SHALL 在 service-toggle 列表中渲染 qwen 卡片，配色变量为 `--qwen: #7c3aed` / `--qwen-soft: rgba(124, 58, 237, 0.10)`（紫色，区分于现有三色）。
2. WHEN `build_service_dashboard("qwen")` 被调用 THEN 它 SHALL 复用现有逻辑返回 stats、active_keys、tokens 等结构，与 tavily/firecrawl 形状一致。
3. WHEN `sync_usage_cache(service="qwen")` 被调用 THEN 它 SHALL 仿照 [server.py:1014 附近的 exa 分支](/Users/chenp/dev/MySearch-Proxy/proxy/server.py)，直接返回 `{"supported": False, "detail": "通义 DashScope 未提供官方用量查询接口"}`，不发起任何 HTTP 请求。
4. WHEN `build_usage_sync_meta_for_dashboard("qwen", ...)` 被调用 THEN 它 SHALL 返回 `supported=False`，控制台对应卡片 SHALL 显示「实时额度暂时无法查询」提示文案。
5. WHEN 控制台调用 `/api/keys?service=qwen` 创建 key THEN 后端 SHALL 接受 `service=qwen` 参数（依赖需求 3.1 的 `SUPPORTED_SERVICES` 扩展）。
6. WHEN dashboard 聚合调用 `asyncio.gather` 拉取各服务 stats（[server.py:1000](/Users/chenp/dev/MySearch-Proxy/proxy/server.py)）THEN 调用列表 SHALL 增加 `build_service_dashboard("qwen", auto_sync=auto_sync)`，返回字典加上 `"qwen"` 键。

---

### 需求 8 · 测试覆盖与文档更新

**用户故事：** 作为后续维护者（或未来的我），我希望关键路径有测试保护，且文档说明如何启用通义 fallback。

#### 验收标准

1. WHEN 运行 `tests/test_clients.py` THEN 测试 SHALL 至少包含 3 个 qwen 用例：(a) `provider="qwen"` 显式调用 mock 成功路径；(b) `_normalize_qwen_results()` 处理空 results 的退化路径；(c) `_dedupe_answers()` Jaccard 去重的两个分支。
2. WHEN 运行 `tests/test_comprehensive.py` THEN 测试 SHALL 至少包含 1 个 fallback 集成用例：mock 出 tavily 池为空 + qwen 池有 key，断言 `/api/search` 返回 200 且响应头含 `X-Provider: qwen`。
3. WHEN 项目根 [README.md](/Users/chenp/dev/MySearch-Proxy/README.md) / [README_EN.md](/Users/chenp/dev/MySearch-Proxy/README_EN.md) 被查看 THEN 它们 SHALL 在 provider 支持矩阵章节列出 qwen，并标注「fork-only feature, not in upstream」。
4. WHEN [docs/mysearch-architecture.md](/Users/chenp/dev/MySearch-Proxy/docs/mysearch-architecture.md) 被查看 THEN「当前边界」章节 SHALL 增补一条「Qwen / DashScope（仅 web，作为 Tavily 耗尽后的 proxy 层 fallback）」。
5. WHEN [proxy/.env.example](/Users/chenp/dev/MySearch-Proxy/proxy/.env.example) 被查看 THEN 它 SHALL 新增 `QWEN_FALLBACK_ENABLED=true` 开关说明（默认开启，便于一键关闭智能 fallback 退回到旧透传行为）。
6. IF 用户设置 `QWEN_FALLBACK_ENABLED=false` THEN 需求 4 的 fallback 流程 SHALL 被禁用，`/api/search` 在 tavily 池空时仍返回 503（保持上游兼容行为）。

---

## 关键设计决策摘录（用户已确认）

| 编号 | 决策 |
|---|---|
| **Q1** 能力定位 | (a) qwen 当 tavily 同级备选，仅在 tavily 全配额耗尽时启用 |
| **Q2** 路由层级 | proxy 层做智能网关，mysearch 客户端零改造（但保留显式 `provider="qwen"` 入口） |
| **Q3** 模型摘要 | 复用 `answer` 字段，并做 Jaccard 去重 |
| **Q4** 上游策略 | 私有 fork，不提 PR，新代码尽量隔离到独立模块便于 rebase |

---

## 风险与边界

| 风险点 | 缓解措施 |
|---|---|
| 通义 web search API 路径/字段变更 | 翻译逻辑集中在 `qwen.py` 单文件，便于版本切换 |
| Tavily 临时网络故障被误判为「池空」 | `consecutive_fails ≥ 3` 才禁用（已是现有逻辑），fallback 不会因瞬时抖动错误触发 |
| 通义模型摘要含敏感拒答 | 需求 6.3 明确过滤拒答模板 |
| 控制台无实时额度，易超额 | 需求 7.4 文案提示，运维方需手动跟进；可选后续加月度调用计数告警 |
| Rebase 上游冲突 | 改动集中在新建文件（`proxy/providers/qwen.py`、`mysearch/providers/qwen.py`），主干仅做枚举/dispatch 一行级改动 |

