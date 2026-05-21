"""Qwen / DashScope provider 适配层（mysearch 客户端侧）。

本模块负责：

- ``build_qwen_request``：构造 DashScope ``text-generation`` + ``tools=web_search``
  请求体，承担"通义把搜索能力挂在文本生成接口下"这一与 Tavily/Firecrawl 风格不同的事实。
- ``normalize_qwen_results``：把通义原生响应（``output.text`` + ``output.search_results``）
  归一化为 MySearch 标准 schema（``answer`` / ``results`` / ``citations`` / ``provider``）。
- ``dedupe_answers``：在客户端 ``verify`` / ``deep`` 路径下，对 Tavily answer 与
  通义 answer 做 Jaccard 去重，避免下游 agent 看到重复信息。

设计动机：所有翻译细节集中在本文件，便于通义 API 字段升级时定点修改，主干
``clients.py`` 仅保留 dispatch 入口。
"""

from __future__ import annotations

import re
from typing import Any

# 通义模型摘要里如果出现这些片段（且文本非常短），视为"拒答模板"，
# 该 answer 不应该污染下游聚合结果。
QWEN_REFUSAL_MARKERS: tuple[str, ...] = (
    "很抱歉",
    "无法回答",
    "抱歉，我无法",
    "I'm sorry",
    "I cannot",
    "I can't",
)

# 拒答检测的字符串长度阈值：超过这个长度的回答即使含拒答关键字也保留，
# 因为可能是真实回答中的语气片段而非纯拒答。
QWEN_REFUSAL_MAX_LEN = 80

# Jaccard 去重阈值：≥ 此值则视为重复，仅保留 Tavily。
DEFAULT_JACCARD_THRESHOLD = 0.6


def build_qwen_request(
    query: str,
    *,
    max_results: int = 5,
    model: str = "qwen-plus",
    enable_search: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    """构造发往 DashScope ``text-generation`` 的请求体。

    DashScope 的 web search 不是独立 REST，而是文本生成接口的一个 tool。
    这里返回 DashScope 期望的 JSON 结构；调用方负责加上 ``Authorization: Bearer ...``
    头并 POST 到 ``/api/v1/services/aigc/text-generation/generation``。
    """
    if not query or not query.strip():
        raise ValueError("qwen.build_qwen_request: query must not be empty")

    body: dict[str, Any] = {
        "model": model,
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": query.strip(),
                }
            ],
        },
        "parameters": {
            # DashScope 老接口字段：内置 web 搜索增强
            "enable_search": bool(enable_search),
            # 部分模型/版本通过 search_options 控制召回条数
            "search_options": {
                "forced_search": True,
                "search_strategy": "max",
                "enable_source": True,
                "enable_citation": True,
                "max_results": max(1, min(int(max_results or 5), 10)),
            },
            "result_format": "message",
        },
    }

    # 允许调用方扩展 parameters（如 temperature 等）
    extra_params = extra.pop("parameters", None)
    if isinstance(extra_params, dict):
        body["parameters"].update(extra_params)

    return body


def _looks_like_refusal(text: str) -> bool:
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    if len(stripped) > QWEN_REFUSAL_MAX_LEN:
        return False
    for marker in QWEN_REFUSAL_MARKERS:
        if marker in stripped:
            return True
    return False


def _extract_qwen_text(payload: dict[str, Any]) -> str:
    """通义 text-generation 的回答可能落在多种字段，做一次容错抽取。"""
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return ""

    # 1) result_format=message 时：output.choices[0].message.content
    choices = output.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        message = first.get("message") if isinstance(first, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
            # content 可能是 list（多模态），拼接其中文本
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
                if parts:
                    return "\n".join(parts)

    # 2) result_format=text 时：output.text
    text = output.get("text")
    if isinstance(text, str):
        return text

    return ""


def _extract_qwen_search_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    output = payload.get("output") or {}
    if not isinstance(output, dict):
        return []

    candidates = output.get("search_results")
    if isinstance(candidates, list):
        return [item for item in candidates if isinstance(item, dict)]

    # 兼容部分版本把搜索结果挂在 references 字段
    references = output.get("references")
    if isinstance(references, list):
        return [item for item in references if isinstance(item, dict)]

    return []


def normalize_qwen_results(payload: dict[str, Any]) -> dict[str, Any]:
    """把通义原生响应归一化为 MySearch 标准 schema。

    - ``answer``：来自 ``output.choices[0].message.content`` 或 ``output.text``，
      若识别为拒答模板则置空。
    - ``results``：``[{title, url, content, score}]``，从 ``output.search_results``
      映射，缺失字段用空字符串/None 兜底，**永远不抛异常**。
    - ``citations``：与 ``results`` 同源，仅含 ``url`` / ``title`` 两字段，便于下游
      与 Tavily citations 拼接。
    - ``provider``：固定为 ``"qwen"``。
    """
    if not isinstance(payload, dict):
        payload = {}

    raw_text = _extract_qwen_text(payload)
    answer = "" if _looks_like_refusal(raw_text) else raw_text.strip()

    raw_results = _extract_qwen_search_results(payload)
    results: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
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
        if isinstance(content, str):
            content = content.strip()
        else:
            content = str(content)

        score = item.get("score")
        if not isinstance(score, (int, float)):
            score = None

        if not url and not title:
            continue

        results.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "score": score,
            }
        )
        if url:
            citations.append({"url": url, "title": title})

    return {
        "provider": "qwen",
        "answer": answer,
        "results": results,
        "citations": citations,
    }


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _tokenize_for_jaccard(text: str) -> set[str]:
    if not text:
        return set()
    return {match.group(0).lower() for match in _TOKEN_RE.finditer(text)}


def _jaccard(a: str, b: str) -> float:
    set_a = _tokenize_for_jaccard(a)
    set_b = _tokenize_for_jaccard(b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return inter / union


def dedupe_answers(
    tavily_answer: str,
    qwen_answer: str,
    *,
    threshold: float = DEFAULT_JACCARD_THRESHOLD,
) -> str:
    """在 verify/deep 模式下合并两路 answer。

    - 任一为空 → 返回非空那个；
    - 两者非空且 Jaccard ≥ threshold → 视为重复，保留 Tavily；
    - 否则拼接为 ``"{tavily}\\n\\n[通义补充]: {qwen}"``。
    """
    tavily = (tavily_answer or "").strip()
    qwen = (qwen_answer or "").strip()
    if not tavily:
        return qwen
    if not qwen:
        return tavily
    if _jaccard(tavily, qwen) >= threshold:
        return tavily
    return f"{tavily}\n\n[通义补充]: {qwen}"


__all__ = [
    "build_qwen_request",
    "normalize_qwen_results",
    "dedupe_answers",
    "DEFAULT_JACCARD_THRESHOLD",
]
