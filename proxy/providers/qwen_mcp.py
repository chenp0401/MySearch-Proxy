"""Proxy 侧 Qwen MCP gateway 适配层。

把 MySearch Proxy 暴露成一个标准的 streamable-http MCP server，让 Claude Code /
Cursor 等原生 MCP 客户端可以**用 mysp-xxx token + pool 化的百炼 key**访问
``bailian_web_search`` 工具，而不是各自直连 DashScope。

设计原则
--------
- **轻量、无状态**：百炼端 MCP 实测对 ``mcp-session-id`` 不敏感，本网关也不维护会话。
- **静态元信息本地伪造**：``initialize`` / ``tools/list`` 不真打百炼（避免浪费上游
  调用、降低 RTT），把固化的工具 schema 直接返回。``tools/call`` 才透传，并在此处
  做 pool 轮询、计费、失败计数。
- **协议兼容性**：返回值严格遵循 MCP 2024-11-05 草案，``content[]`` 用 ``type: text``
  携带百炼原始 JSON 字符串（与百炼自身行为一致）。

不在本模块负责
--------------
- token 鉴权（在 server.py 路由层完成，本模块只接受 ``token_row``）
- httpx client 生命周期管理（由 server.py 注入）
"""

from __future__ import annotations

import time
from typing import Any

import httpx

# DashScope 官方 WebSearch MCP 端点（百炼控制台开通"联网搜索 MCP"后即可使用）
DASHSCOPE_MCP_PATH = "/api/v1/mcps/WebSearch/mcp"

# 网关声明的 MCP server 元信息（initialize 响应里返回给客户端）
GATEWAY_SERVER_NAME = "MySearch-Qwen-Gateway"
GATEWAY_SERVER_VERSION = "1.0.0"
GATEWAY_PROTOCOL_VERSION = "2024-11-05"

# 暴露给客户端的工具 schema —— 直接复用百炼 bailian_web_search 的字段定义。
# 本地固化的好处：tools/list 不打上游，零延迟、零额度消耗。
BAILIAN_WEB_SEARCH_TOOL = {
    "name": "bailian_web_search",
    "description": "搜索可用于查询百科知识、时事新闻、天气等信息（通过 MySearch Proxy 网关，按 mysp- token 计费）",
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {
                "description": "user query in the format of string",
                "title": "Query",
                "type": "string",
            },
            "count": {
                "default": 5,
                "description": "number of search results",
                "title": "Count",
                "type": "integer",
            },
        },
        "required": ["query"],
    },
}


def build_initialize_result() -> dict[str, Any]:
    """构造 ``initialize`` 响应的 ``result`` 段。"""
    return {
        "protocolVersion": GATEWAY_PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": GATEWAY_SERVER_NAME,
            "version": GATEWAY_SERVER_VERSION,
        },
    }


def build_tools_list_result() -> dict[str, Any]:
    """构造 ``tools/list`` 响应的 ``result`` 段。"""
    return {"tools": [BAILIAN_WEB_SEARCH_TOOL]}


def jsonrpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def call_dashscope_mcp(
    http_client: httpx.AsyncClient,
    *,
    key: str,
    payload: dict[str, Any],
    base_url: str,
) -> tuple[httpx.Response, float]:
    """把已组装好的 JSON-RPC payload 透传到 DashScope WebSearch MCP。

    - 自动注入 ``Authorization: Bearer {key}``。
    - ``Accept`` 同时带 ``application/json, text/event-stream``，与百炼握手要求一致。
    - 返回 ``(httpx.Response, elapsed_seconds)``，由调用方判定 status_code。
    """
    if not key:
        raise ValueError("call_dashscope_mcp: key must not be empty")

    url = f"{base_url.rstrip('/')}{DASHSCOPE_MCP_PATH}"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    start = time.time()
    resp = await http_client.post(url, json=payload, headers=headers)
    elapsed = time.time() - start
    return resp, elapsed


__all__ = [
    "DASHSCOPE_MCP_PATH",
    "GATEWAY_SERVER_NAME",
    "GATEWAY_SERVER_VERSION",
    "GATEWAY_PROTOCOL_VERSION",
    "BAILIAN_WEB_SEARCH_TOOL",
    "build_initialize_result",
    "build_tools_list_result",
    "jsonrpc_result",
    "jsonrpc_error",
    "call_dashscope_mcp",
]
