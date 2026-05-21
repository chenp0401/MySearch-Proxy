"""MySearch provider 子模块集合。

私有 fork 新增 provider（如 Qwen）的实现集中在此包内，主干 clients.py 仅
保留显式 dispatch 入口，便于未来 rebase 上游时合并冲突最小化。
"""

from mysearch.providers import qwen as qwen

__all__ = ["qwen"]
