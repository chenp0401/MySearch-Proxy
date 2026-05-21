"""通义 / DashScope provider 适配层单元测试。

覆盖：
- ``mysearch.providers.qwen`` 三大公开函数：build_qwen_request /
  normalize_qwen_results / dedupe_answers。
- ``proxy.providers.qwen`` 的 translate_tavily_to_qwen_request /
  translate_qwen_to_tavily_response 翻译两端。

注：本文件用 ``unittest`` 风格与仓库其他测试保持一致。
"""

from __future__ import annotations

import os
import sys
import unittest

# 让 tests 目录可以同时导入 mysearch 和 proxy 两个包根。
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "proxy"))

from mysearch.providers import qwen as ms_qwen  # noqa: E402

# proxy 包采用「目录作为运行根」的风格（from key_pool import pool 等），
# 这里直接 import providers.qwen 模块以避免顶层 server.py 副作用。
# httpx 是 proxy 的运行时依赖；本仓库 mysearch 测试不强依赖它，
# 所以在缺失 httpx 时优雅跳过 proxy 段而不是让整个测试文件加载失败。
try:
    from providers import qwen as proxy_qwen  # noqa: E402

    PROXY_QWEN_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - depends on test env
    proxy_qwen = None  # type: ignore[assignment]
    PROXY_QWEN_AVAILABLE = False


class MySearchQwenBuildRequestTests(unittest.TestCase):
    def test_build_qwen_request_minimal(self) -> None:
        body = ms_qwen.build_qwen_request(query="OpenAI 最新发布")
        self.assertEqual(body["model"], "qwen-plus")
        messages = body["input"]["messages"]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"], "OpenAI 最新发布")
        params = body["parameters"]
        self.assertTrue(params["enable_search"])
        self.assertEqual(params["search_options"]["max_results"], 5)
        self.assertEqual(params["result_format"], "message")

    def test_build_qwen_request_clamps_max_results(self) -> None:
        body = ms_qwen.build_qwen_request(query="x", max_results=99)
        self.assertEqual(body["parameters"]["search_options"]["max_results"], 10)
        # max_results=0 在 mysearch 侧被视为"缺失"，使用默认 5（保留与
        # 其他 provider 一致的"0/None → 默认"语义）。
        body2 = ms_qwen.build_qwen_request(query="x", max_results=0)
        self.assertEqual(body2["parameters"]["search_options"]["max_results"], 5)

    def test_build_qwen_request_rejects_empty_query(self) -> None:
        with self.assertRaises(ValueError):
            ms_qwen.build_qwen_request(query="   ")


class MySearchQwenNormalizeTests(unittest.TestCase):
    def test_normalize_extracts_message_content_and_results(self) -> None:
        payload = {
            "output": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "OpenAI 在 2026 年 5 月 20 日发布了 GPT-X。",
                        }
                    }
                ],
                "search_results": [
                    {
                        "title": "OpenAI Blog",
                        "url": "https://openai.com/blog/gpt-x",
                        "content": "Today we are launching GPT-X.",
                        "score": 0.92,
                    },
                    {
                        "title": "Reuters Coverage",
                        "url": "https://reuters.com/openai-gpt-x",
                        "snippet": "Reuters reports the launch.",
                    },
                ],
            }
        }
        result = ms_qwen.normalize_qwen_results(payload)

        self.assertEqual(result["provider"], "qwen")
        self.assertIn("GPT-X", result["answer"])
        self.assertEqual(len(result["results"]), 2)
        first = result["results"][0]
        self.assertEqual(first["url"], "https://openai.com/blog/gpt-x")
        self.assertEqual(first["title"], "OpenAI Blog")
        self.assertEqual(first["content"], "Today we are launching GPT-X.")
        self.assertEqual(first["score"], 0.92)
        # 第二条用 snippet 兜底
        self.assertEqual(result["results"][1]["content"], "Reuters reports the launch.")
        # citations 与 results 同源
        self.assertEqual(len(result["citations"]), 2)
        self.assertEqual(result["citations"][0]["url"], "https://openai.com/blog/gpt-x")

    def test_normalize_empty_results_returns_empty_list_no_exception(self) -> None:
        payload = {
            "output": {
                "choices": [
                    {"message": {"content": "I don't have search results to share."}}
                ],
                "search_results": [],
            }
        }
        result = ms_qwen.normalize_qwen_results(payload)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["citations"], [])
        # 长文本不被识别为拒答
        self.assertIn("search results", result["answer"])

    def test_normalize_refusal_template_blanks_answer(self) -> None:
        payload = {
            "output": {
                "choices": [{"message": {"content": "很抱歉，我无法回答。"}}],
                "search_results": [],
            }
        }
        result = ms_qwen.normalize_qwen_results(payload)
        self.assertEqual(result["answer"], "")

    def test_normalize_garbage_payload_does_not_raise(self) -> None:
        # 极端容错：payload 不是 dict / output 不存在
        self.assertEqual(ms_qwen.normalize_qwen_results({}), {
            "provider": "qwen",
            "answer": "",
            "results": [],
            "citations": [],
        })
        self.assertEqual(
            ms_qwen.normalize_qwen_results(None),  # type: ignore[arg-type]
            {"provider": "qwen", "answer": "", "results": [], "citations": []},
        )

    def test_normalize_text_format_fallback(self) -> None:
        payload = {"output": {"text": "A simple answer.", "search_results": []}}
        result = ms_qwen.normalize_qwen_results(payload)
        self.assertEqual(result["answer"], "A simple answer.")


class MySearchQwenDedupeAnswersTests(unittest.TestCase):
    def test_dedupe_high_jaccard_keeps_tavily(self) -> None:
        tavily = "OpenAI launched GPT-X on May 20, with new reasoning capabilities."
        qwen = "OpenAI launched GPT-X on May 20 with new reasoning capabilities."
        merged = ms_qwen.dedupe_answers(tavily, qwen)
        self.assertEqual(merged, tavily)

    def test_dedupe_low_jaccard_concatenates(self) -> None:
        tavily = "OpenAI announced GPT-X on May 20."
        qwen = "百度发布了文心一言 5。"
        merged = ms_qwen.dedupe_answers(tavily, qwen)
        self.assertIn(tavily, merged)
        self.assertIn("[通义补充]", merged)
        self.assertIn("百度", merged)

    def test_dedupe_empty_inputs(self) -> None:
        self.assertEqual(ms_qwen.dedupe_answers("", "qwen only"), "qwen only")
        self.assertEqual(ms_qwen.dedupe_answers("tavily only", ""), "tavily only")
        self.assertEqual(ms_qwen.dedupe_answers("", ""), "")


class ProxyQwenTranslateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not PROXY_QWEN_AVAILABLE:
            raise unittest.SkipTest("proxy.providers.qwen 不可用（httpx 未安装）")

    def test_translate_tavily_to_qwen_request_basic(self) -> None:
        tavily_body = {"query": "GPT-X 发布", "max_results": 7}
        body = proxy_qwen.translate_tavily_to_qwen_request(tavily_body)
        self.assertEqual(body["model"], proxy_qwen.DEFAULT_QWEN_MODEL)
        self.assertEqual(body["parameters"]["search_options"]["max_results"], 7)
        self.assertEqual(body["input"]["messages"][0]["content"], "GPT-X 发布")

    def test_translate_tavily_to_qwen_request_includes_domain_hints(self) -> None:
        tavily_body = {
            "query": "OpenAI GPT-X",
            "include_domains": ["openai.com"],
            "exclude_domains": ["news.ycombinator.com"],
        }
        body = proxy_qwen.translate_tavily_to_qwen_request(tavily_body)
        prompt = body["input"]["messages"][0]["content"]
        self.assertIn("openai.com", prompt)
        self.assertIn("news.ycombinator.com", prompt)

    def test_translate_tavily_to_qwen_request_rejects_empty_query(self) -> None:
        with self.assertRaises(ValueError):
            proxy_qwen.translate_tavily_to_qwen_request({"query": ""})
        with self.assertRaises(ValueError):
            proxy_qwen.translate_tavily_to_qwen_request("not-a-dict")  # type: ignore[arg-type]

    def test_translate_qwen_to_tavily_response_full(self) -> None:
        qwen_payload = {
            "request_id": "abc123",
            "output": {
                "choices": [{"message": {"content": "GPT-X 发布详情。"}}],
                "search_results": [
                    {
                        "title": "OpenAI Blog",
                        "url": "https://openai.com/blog/gpt-x",
                        "content": "Launching GPT-X.",
                    }
                ],
            },
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        result = proxy_qwen.translate_qwen_to_tavily_response(
            qwen_payload, query="GPT-X 发布", elapsed_seconds=0.42
        )
        self.assertEqual(result["query"], "GPT-X 发布")
        self.assertEqual(result["answer"], "GPT-X 发布详情。")
        self.assertEqual(len(result["results"]), 1)
        self.assertEqual(result["results"][0]["url"], "https://openai.com/blog/gpt-x")
        self.assertAlmostEqual(result["response_time"], 0.42, places=3)
        self.assertEqual(result["request_id"], "abc123")
        self.assertEqual(result["usage"]["input_tokens"], 100)

    def test_translate_qwen_to_tavily_response_blanks_refusal(self) -> None:
        qwen_payload = {
            "output": {
                "choices": [{"message": {"content": "很抱歉，我无法回答这个问题。"}}],
                "search_results": [],
            }
        }
        result = proxy_qwen.translate_qwen_to_tavily_response(qwen_payload)
        self.assertEqual(result["answer"], "")
        self.assertEqual(result["results"], [])


if __name__ == "__main__":
    unittest.main()
