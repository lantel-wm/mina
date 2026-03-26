from __future__ import annotations

import unittest

from mina_agent.memories.citations import (
    get_thread_ids_from_citations,
    parse_memory_citation,
    strip_memory_citations,
)


class MemoryCitationTests(unittest.TestCase):
    def test_strip_memory_citations_returns_visible_text_and_blocks(self) -> None:
        text = (
            "你好，我记得这件事。\n"
            "<oai-mem-citation>\n"
            "<citation_entries>\n"
            "MEMORY.md:10-18|note=[player preference]\n"
            "</citation_entries>\n"
            "<thread_ids>\n"
            "thread-1\n"
            "</thread_ids>\n"
            "</oai-mem-citation>"
        )

        visible, citations = strip_memory_citations(text)

        self.assertEqual(visible, "你好，我记得这件事。")
        self.assertEqual(len(citations), 1)

    def test_parse_memory_citation_extracts_entries_and_thread_ids(self) -> None:
        citations = [
            "<oai-mem-citation>\n"
            "<citation_entries>\n"
            "MEMORY.md:10-18|note=[player preference]\n"
            "rollout_summaries/thread-1.md:1-6|note=[recent memory]\n"
            "</citation_entries>\n"
            "<thread_ids>\n"
            "thread-1\n"
            "thread-2\n"
            "</thread_ids>\n"
            "</oai-mem-citation>"
        ]

        parsed = parse_memory_citation(citations)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["entries"][0]["path"], "MEMORY.md")
        self.assertEqual(parsed["entries"][0]["line_start"], 10)
        self.assertEqual(parsed["entries"][0]["line_end"], 18)
        self.assertEqual(parsed["entries"][0]["note"], "player preference")
        self.assertEqual(parsed["thread_ids"], ["thread-1", "thread-2"])

    def test_get_thread_ids_supports_legacy_rollout_ids_block(self) -> None:
        citations = [
            "<oai-mem-citation>\n"
            "<rollout_ids>\n"
            "thread-legacy\n"
            "</rollout_ids>\n"
            "</oai-mem-citation>"
        ]

        self.assertEqual(get_thread_ids_from_citations(citations), ["thread-legacy"])


if __name__ == "__main__":
    unittest.main()
