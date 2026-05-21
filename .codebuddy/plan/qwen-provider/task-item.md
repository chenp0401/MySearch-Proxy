# 实施计划 · 通义（Qwen / DashScope）Provider 接入

> 基于：[requirements.md](./requirements.md)
> 改动隔离原则：尽量集中在新建文件 `mysearch/providers/qwen.py` 与 `proxy/providers/qwen.py`，主干 (`clients.py` / `server.py` / `database.py` / `config.py`) 仅做枚举扩展与 dispatch 接入。

---

- [ ] 1. 扩展 provider 元数据与配置加载
   - 在 `mysearch/config.py` 的 `MySearchConfig` dataclass 中新增 `qwen: ProviderConfig` 字段，并在 `from_env()` 中按 Tavily 段同构方式解析 `MYSEARCH_QWEN_BASE_URL` / `MYSEARCH_QWEN_API_KEY` / `MYSEARCH_QWEN_API_KEYS` / `MYSEARCH_QWEN_KEYS_FILE` / `MYSEARCH_QWEN_AUTH_*`，默认 `base_url=https://dashscope.aliyuncs.com`，`auth_mode=bearer`，`search` 路径在直连时为 `/api/v1/services/aigc/text-generation/generation`、走 proxy 时为 `/qwen/search`
   - 在 `mysearch/clients.py` 顶部把 `ProviderName` 扩展为 `Literal["auto", "tavily", "firecrawl", "exa", "xai", "qwen"]`
   - 在 `mysearch/.env.example` Exa 段后追加完整 `MYSEARCH_QWEN_*` 示例段
   - _需求：1.1, 1.2, 1.3, 1.4, 1.5_

- [ ] 2. 新建 `mysearch/providers/qwen.py` 实现请求构造与响应归一化
   - 创建 `mysearch/providers/__init__.py` 包定义
   - 实现 `build_qwen_request(query, *, max_results, **kwargs) -> dict` 构造 DashScope `text-generation` 带 `tools=[{"type":"web_search"}]` 的请求体
   - 实现 `normalize_qwen_results(payload) -> dict` 把 `output.text` → `answer`、`output.search_results` → `results[{title,url,content,score}]`、`citations`、`provider="qwen"`，对空 results / 拒答模板（`"很抱歉"` / `"无法回答"`）做退化处理（`answer=""`，`results=[]` 不抛异常）
   - 实现 `dedupe_answers(tavily_answer, qwen_answer) -> str`：Jaccard ≥ 0.6 保留 Tavily，否则拼接 `"{tavily}\n\n[通义补充]: {qwen}"`
   - _需求：2.2, 2.3, 6.1, 6.2, 6.3_

- [ ] 3. 在 `mysearch/clients.py` 接入 qwen 显式调用分支
   - 新增 `_search_qwen(self, query, **kwargs)` 方法，复用 `_request_json` + `ProviderConfig`，调用 `mysearch.providers.qwen.build_qwen_request` 与 `normalize_qwen_results`
   - 在主 `search()` 入口的 provider dispatch 处增加 `if provider == "qwen": return self._search_qwen(...)` 分支（参考现有 `tavily/firecrawl/exa/xai` 分支）
   - 在 `health()` 的 `providers` dict 中新增 qwen 项（复用 `_describe_provider`）
   - 在 `_get_key_or_raise` 等需要 provider name 判断的位置补全 qwen 分支，401/403 抛 `MySearchHTTPError(provider="qwen", is_auth_error=True)`
   - 显式约束：`provider="auto"` 时**不**自动选 qwen（保持现有 web→tavily 默认）
   - _需求：2.1, 2.4, 2.5, 2.6_

- [ ] 4. 扩展 proxy 数据库支持 qwen service
   - 在 `proxy/database.py` 中：
     - `SUPPORTED_SERVICES` 改为 `("tavily", "firecrawl", "exa", "qwen")`
     - `TOKEN_PREFIX["qwen"] = "qwen-"`
     - `KEY_PATTERNS["qwen"] = r"(sk-[A-Za-z0-9]{32,})"`
   - 验证 `db.init_db()` 在已有 v0.1.13 数据库上启动时通过 `_ensure_service_columns` 自动兼容，无需写手动迁移脚本
   - 不修改 `proxy/key_pool.py`（其循环 `SUPPORTED_SERVICES` 自动包含 qwen）
   - _需求：3.1, 3.2, 3.3, 3.4, 3.5_

- [ ] 5. 新建 `proxy/providers/qwen.py` 实现请求/响应翻译与上游调用
   - 创建 `proxy/providers/__init__.py` 包定义
   - 实现 `translate_tavily_to_qwen_request(tavily_body) -> dict` 把 `{query, max_results, ...}` 转为 DashScope 请求体
   - 实现 `translate_qwen_to_tavily_response(qwen_payload) -> dict` 翻译回 Tavily schema：`answer` ← `output.text`（应用拒答过滤）、`results[].url/title/content` ← `output.search_results`、补 `response_time` 字段
   - 实现 `async def call_qwen_upstream(http_client, key, body) -> httpx.Response` 封装上游调用（`Authorization: Bearer {key}`，超时与现有 Tavily 调用一致）
   - _需求：4.2, 4.3, 6.1, 6.3_

- [ ] 6. 在 `proxy/server.py` `/api/search` 中接入 Tavily→Qwen 智能 fallback
   - 修改 `proxy_tavily()` 函数（`server.py` ~1654 行）：当 `endpoint == "search"` 且 `pool.get_next_key("tavily")` 返回 `None` 时，改为调用新增的 `_try_qwen_fallback(token_row, body)` helper
   - `_try_qwen_fallback`：取 `pool.get_next_key("qwen")`；为空则返回 503 `{"detail": "all providers exhausted"}`；否则调用 `proxy.providers.qwen.call_qwen_upstream` + `translate_qwen_to_tavily_response`
   - 成功响应附加 HTTP header `X-Provider: qwen`；失败时调 `pool.report_result("qwen", ..., False)` 并抛 502
   - 在 `usage_logs` 写入 `service="qwen"`, `endpoint="search"` 一行
   - 当 `endpoint == "extract"` 且 tavily 池空时**不**触发 fallback，返回 503 `"extract not supported by fallback providers"`
   - 增加 `QWEN_FALLBACK_ENABLED` 环境变量读取（默认 `true`），为 `false` 时跳过 fallback 直接走原 503 路径
   - _需求：4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 8.6_

- [ ] 7. 在 `proxy/server.py` 新增显式 `/qwen/search` 端点
   - 仿 `proxy_exa_search`（`server.py` ~1724 行）实现 `proxy_qwen_search(request)`
   - 流程：`extract_token` → `get_token_row_or_401(..., "qwen")` → `pool.get_next_key("qwen")`（空则 503）→ 调 `proxy.providers.qwen.call_qwen_upstream`（透传，不翻译）→ `pool.report_result` → `db.log_usage(service="qwen", endpoint="search")` → 透传上游 JSON
   - _需求：5.1, 5.2, 5.3, 5.4_

- [ ] 8. 接入 proxy 控制台 dashboard 与额度同步降级
   - 在 `proxy/server.py` 中：
     - `sync_usage_cache` 增加 `service == "qwen"` 分支，照搬 exa 模式返回 `{"supported": False, "detail": "通义 DashScope 未提供官方用量查询接口", ...}`
     - `build_usage_sync_meta_for_dashboard` 增加 qwen 分支返回 `supported=False`，提示文案「实时额度暂时无法查询」
     - 在 dashboard 聚合的 `asyncio.gather(...)` 调用列表（~`server.py:1000`）追加 `build_service_dashboard("qwen", auto_sync=auto_sync)`，结果 dict 加 `"qwen"` 键
   - _需求：7.2, 7.3, 7.4, 7.5, 7.6_

- [ ] 9. 扩展 `proxy/templates/console.html` 控制台 UI
   - 在 CSS `:root` 块新增 `--qwen: #7c3aed` 与 `--qwen-soft: rgba(124, 58, 237, 0.10)`
   - 复制 `.service-chip[data-service="firecrawl"]` / `.service-toggle[data-service="firecrawl"]` 等规则为 `data-service="qwen"` 版本（chip / toggle / panel 三处）
   - 在 service 切换列表 HTML 模板里追加 qwen 卡片（含 fallback 状态、key 数、提示「无实时额度」）
   - _需求：7.1_

- [ ] 10. 测试与文档更新
   - 在 `tests/test_clients.py` 增加 3 个 qwen 单测：(a) `provider="qwen"` 显式调用 mock 成功路径；(b) `normalize_qwen_results` 空 results / 拒答模板退化；(c) `dedupe_answers` Jaccard 高/低相似度两个分支
   - 在 `tests/test_comprehensive.py` 增加 1 个集成用例：mock tavily 池空 + qwen 池有 key，断言 `POST /api/search` 返回 200、响应头含 `X-Provider: qwen`、body 含翻译后的 `answer`/`results`；并补 1 个 `QWEN_FALLBACK_ENABLED=false` 时仍返回 503 的反向用例
   - 更新 `README.md` / `README_EN.md` provider 矩阵增列 qwen，标注「fork-only feature, not in upstream」
   - 更新 `docs/mysearch-architecture.md`「当前边界」章节增补「Qwen / DashScope（仅 web，作为 Tavily 耗尽后的 proxy 层 fallback）」
   - 更新 `proxy/.env.example` 增加 `QWEN_FALLBACK_ENABLED=true` 开关说明
   - _需求：8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

