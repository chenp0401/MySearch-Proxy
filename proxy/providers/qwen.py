"""Proxy 侧 Qwen / DashScope 适配层。

本模块负责：

- ``QWEN_API_BASE`` / ``QWEN_GENERATION_PATH``：DashScope 文本生成 + web search 接口。
- ``translate_tavily_to_qwen_request``：把 Tavily 兼容请求体（``query`` / ``max_results``
  等）翻译成 DashScope 的 ``text-generation`` + ``tools=web_search`` 请求结构。
  这是「Tavily→Qwen 智能 fallback」路径必须的。
- ``translate_qwen_to_tavily_response``：把通义原生响应翻译回 Tavily schema
  （``answer`` / ``results[].url|title|content`` / ``response_time``），让上游调用
  方完全无感知 fallback。
- ``call_qwen_upstream``：封装一次上游 HTTP 调用，统一注入 ``Authorization: Bearer {key}``，
  超时与现有 Tavily/Firecrawl 行为一致（依赖外部传入的 ``httpx.AsyncClient``）。

设计动机：把 fallback / 显式 ``/qwen/search`` 端点用到的所有翻译逻辑集中在此文件，
``server.py`` 仅做路由 + 鉴权 + 计量。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# DashScope 上游基地址与路径。可通过环境变量覆盖（在 server.py 中拼接最终 URL）。
QWEN_API_BASE = "https://dashscope.aliyuncs.com"
QWEN_GENERATION_PATH = "/api/v1/services/aigc/text-generation/generation"

# 默认模型与召回上限。
DEFAULT_QWEN_MODEL = "qwen-plus"
DEFAULT_QWEN_MAX_RESULTS = 5
QWEN_MAX_RESULTS_LIMIT = 10

# 通义模型摘要里如果出现这些片段（且文本短），视为"拒答模板"，answer 应置空。
QWEN_REFUSAL_MARKERS: tuple[str, ...] = (
    "很抱歉",
    "无法回答",
    "抱歉，我无法",
    "I'm sorry",
    "I cannot",
    "I can't",
)
QWEN_REFUSAL_MAX_LEN = 80


def _clamp_max_results(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_QWEN_MAX_RESULTS
    return max(1, min(n, QWEN_MAX_RESULTS_LIMIT))


def translate_tavily_to_qwen_request(
    tavily_body: dict[str, Any],
    *,
    model: str = DEFAULT_QWEN_MODEL,
) -> dict[str, Any]:
    """把 Tavily 入参翻译成 DashScope 文本生成接口请求体。

    Tavily 常见入参：``query`` / ``max_results`` / ``topic`` / ``include_answer``
    / ``include_domains`` / ``exclude_domains`` 等。通义不支持 domain include/exclude，
    我们把这些信息以自然语言注入到 prompt 里作为软约束。
    """
    if not isinstance(tavily_body, dict):
        raise ValueError("translate_tavily_to_qwen_request: body must be a dict")

    query = (tavily_body.get("query") or "").strip()
    if not query:
        raise ValueError("translate_tavily_to_qwen_request: query must not be empty")

    max_results = _clamp_max_results(tavily_body.get("max_results"))

    # domain 约束以自然语言注入。
    include_domains = tavily_body.get("include_domains") or []
    exclude_domains = tavily_body.get("exclude_domains") or []
    hint_lines: list[str] = []
    if include_domains:
        hint_lines.append(
            "Prefer results from these domains: " + ", ".join(include_domains)
        )
    if exclude_domains:
        hint_lines.append(
            "Avoid results from these domains: " + ", ".join(exclude_domains)
        )

    user_content = query if not hint_lines else query + "\n\n(" + "; ".join(hint_lines) + ")"

    return {
        "model": model,
        "input": {
            "messages": [
                {"role": "user", "content": user_content},
            ],
        },
        "parameters": {
            "enable_search": True,
            "search_options": {
                "forced_search": True,
                "search_strategy": "max",
                "enable_source": True,
                "enable_citation": True,
                "max_results": max_results,
            },
            "result_format": "message",
        },
    }


def _is_refusal(text: str) -> bool:
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) > QWEN_REFUSAL_MAX_LEN:
        return False
    return any(marker in stripped for marker in QWEN_REFUSAL_MARKERS)


def _extract_answer_text(payload: dict[str, Any]) -> str:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return ""

    choices = output.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") if isinstance(first, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
                if parts:
                    return "\n".join(parts)

    text = output.get("text")
    if isinstance(text, str):
        return text
    return ""


def _extract_search_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return []

    candidates = output.get("search_results")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]

    references = output.get("references")
    if isinstance(references, list):
        return [item for item in references if isinstance(item, dict)]

    return []


def translate_qwen_to_tavily_response(
    qwen_payload: dict[str, Any],
    *,
    query: str = "",
    elapsed_seconds: float | None = None,
) -> dict[str, Any]:
    """把通义响应翻译回 Tavily 兼容 schema。

    输出字段：``query`` / ``answer`` / ``results[{title,url,content,score}]``
    / ``response_time``。``answer`` 在识别为拒答模板时置空。
    """
    if not isinstance(qwen_payload, dict):
        qwen_payload = {}

    raw_answer = _extract_answer_text(qwen_payload)
    answer = "" if _is_refusal(raw_answer) else raw_answer.strip()

    raw_results = _extract_search_results(qwen_payload)
    results: list[dict[str, Any]] = []
    for item in raw_results:
        url = (item.get("url") or item.get("link") or "").strip()
        title = (item.get("title") or item.get("name") or "").strip()
        content = (
            item.get("content")
            or item.get("snippet")
            or item.get("summary")
            or item.get("description")
            or ""
        )
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()

        if not url and not title:
            continue

        score = item.get("score")
        if not isinstance(score, (int, float)):
            score = None

        results.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "score": score,
            }
        )

    return {
        "query": query,
        "answer": answer,
        "results": results,
        # 保留通义原始 request_id / usage 便于排查
        "response_time": float(elapsed_seconds) if elapsed_seconds is not None else 0.0,
        "request_id": qwen_payload.get("request_id", ""),
        "usage": qwen_payload.get("usage") or {},
    }


async def call_qwen_upstream(
    http_client: httpx.AsyncClient,
    *,
    key: str,
    body: dict[str, Any],
    base_url: str = QWEN_API_BASE,
    path: str = QWEN_GENERATION_PATH,
) -> tuple[httpx.Response, float]:
    """统一封装上游调用。

    返回 ``(httpx.Response, elapsed_seconds)``，elapsed 用于填入 Tavily schema 的
    ``response_time`` 字段。本函数不抛 HTTPException，由调用方根据 status_code
    做对应处理（与 ``proxy_tavily`` / ``proxy_exa_search`` 现有风格一致）。
    """
    if not key:
        raise ValueError("call_qwen_upstream: key must not be empty")
    if not isinstance(body, dict):
        raise ValueError("call_qwen_upstream: body must be a dict")

    url = f"{base_url.rstrip('/')}{path}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # DashScope 老接口需要这个 header 才能在响应里返回 search_results
        "X-DashScope-DataInspection": "enable",
    }
    start = time.time()
    resp = await http_client.post(url, json=body, headers=headers)
    elapsed = time.time() - start
    return resp, elapsed


__all__ = [
    "QWEN_API_BASE",
    "QWEN_GENERATION_PATH",
    "DEFAULT_QWEN_MODEL",
    "DEFAULT_QWEN_MAX_RESULTS",
    "QWEN_MAX_RESULTS_LIMIT",
    "translate_tavily_to_qwen_request",
    "translate_qwen_to_tavily_response",
    "call_qwen_upstream",
]
