"""Proxy 端 provider 子模块集合。

私有 fork 新增的 provider 适配层放在这里，server.py 仅在路由层引入它们的
公开函数（``call_qwen_upstream`` / ``translate_*``），便于未来 rebase 上游时
合并冲突最小化。
"""

from . import qwen as qwen
from . import qwen_mcp as qwen_mcp

__all__ = ["qwen", "qwen_mcp"]
