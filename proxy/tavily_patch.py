"""
MySearch SDK 透明补丁
=====================

让官方 Skill / SDK 代码**一行不改**地走 MySearch Proxy。

使用方式：在你的 Skill / 应用入口最前面加一行：

    import tavily_patch  # noqa: F401  仅需 import 一次

或在 Skill 启动脚本里设置环境变量后 import：

    export TAVILY_BASE_URL=https://search.uctest.cn        # Tavily 走 MySearch
    export TAVILY_API_KEY=mysp-xxxxxxxxxxxxxxxxxxxxxxxx    # 你的 MySearch token
    export FIRECRAWL_API_URL=https://search.uctest.cn      # Firecrawl 走 MySearch
    export FIRECRAWL_API_KEY=mysp-xxxxxxxxxxxxxxxxxxxxxxxx # 你的 MySearch token

补丁的工作原理
--------------

1. **Tavily** —— `tavily-python` SDK 已经原生支持 `api_base_url` 构造参数，
   但官方 Skill 不会显式传它，所以补丁让 `TavilyClient` / `AsyncTavilyClient`
   在未显式指定 `api_base_url` 时，自动从环境变量 `TAVILY_BASE_URL`（或
   `TAVILY_API_BASE_URL`）读取。

2. **Firecrawl** —— `firecrawl-py` SDK 的 `api_url` 默认硬编码为
   `https://api.firecrawl.dev`，且**不读取环境变量**。补丁让其在未显式指定
   `api_url` 时，自动从环境变量 `FIRECRAWL_API_URL` 读取。

补丁是幂等的：重复 import 不会产生副作用；找不到 SDK 时静默跳过。
"""
from __future__ import annotations

import os
from typing import Any

_PATCHED_FLAG = "_mysearch_patched"


def _patch_tavily() -> bool:
    """让 tavily-python 自动读取 TAVILY_BASE_URL 环境变量。"""
    try:
        import tavily  # type: ignore
    except ImportError:
        return False

    base_url = (
        os.environ.get("TAVILY_BASE_URL")
        or os.environ.get("TAVILY_API_BASE_URL")
    )
    if not base_url:
        return False  # 没设环境变量就不打补丁，保持官方默认行为

    base_url = base_url.rstrip("/")

    def _wrap(cls: Any) -> None:
        if cls is None or getattr(cls, _PATCHED_FLAG, False):
            return
        original_init = cls.__init__

        def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs.setdefault("api_base_url", base_url)
            return original_init(self, *args, **kwargs)

        cls.__init__ = patched_init  # type: ignore[assignment]
        setattr(cls, _PATCHED_FLAG, True)

    _wrap(getattr(tavily, "TavilyClient", None))
    _wrap(getattr(tavily, "AsyncTavilyClient", None))
    return True


def _patch_firecrawl() -> bool:
    """让 firecrawl-py 自动读取 FIRECRAWL_API_URL 环境变量。"""
    try:
        import firecrawl  # type: ignore
    except ImportError:
        return False

    api_url = os.environ.get("FIRECRAWL_API_URL")
    if not api_url:
        return False

    api_url = api_url.rstrip("/")

    def _wrap(cls: Any) -> None:
        if cls is None or getattr(cls, _PATCHED_FLAG, False):
            return
        original_init = cls.__init__

        def patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs.setdefault("api_url", api_url)
            return original_init(self, *args, **kwargs)

        cls.__init__ = patched_init  # type: ignore[assignment]
        setattr(cls, _PATCHED_FLAG, True)

    # firecrawl-py 4.x 的全部公开客户端类
    for name in ("Firecrawl", "AsyncFirecrawl", "FirecrawlApp", "AsyncFirecrawlApp"):
        _wrap(getattr(firecrawl, name, None))
    return True


def apply() -> dict[str, bool]:
    """主动调用以应用补丁，返回各 SDK 的 patch 结果。"""
    return {
        "tavily": _patch_tavily(),
        "firecrawl": _patch_firecrawl(),
    }


# import 时自动应用
_RESULT = apply()

__all__ = ["apply"]
