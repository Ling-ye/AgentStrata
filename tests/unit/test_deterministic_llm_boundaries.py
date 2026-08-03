from __future__ import annotations

from types import SimpleNamespace

from chatcopilot.agent.search.reranker import ResultReranker, prepare_results


class _NoCallLlm:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, **_kwargs):
        self.calls += 1
        return SimpleNamespace(content='{"ranked_findings": []}')


def test_result_preprocessing_deduplicates_canonical_urls_by_source_weight() -> None:
    results = [
        {
            "ok": True,
            "logical_source": "web",
            "actual_source": "searxng",
            "summary": {
                "items": [
                    {
                        "title": "Release Notes",
                        "url": "https://example.org/release/?utm_source=test",
                        "published_at": "2026-01-01",
                    }
                ]
            },
        },
        {
            "ok": True,
            "logical_source": "github",
            "actual_source": "github",
            "summary": {
                "items": [
                    {
                        "title": "Release notes",
                        "url": "https://example.org/release",
                        "published_at": "2025-01-01",
                    }
                ]
            },
        },
    ]

    prepared, decision = prepare_results(results)

    assert prepared[0]["summary"]["items"] == []
    assert len(prepared[1]["summary"]["items"]) == 1
    assert decision == {
        "decision_source": "script",
        "decision_reason": "canonical URL/title deduplication and source/recency ordering",
        "input_items": 2,
        "output_items": 1,
        "duplicates_removed": 1,
    }


def test_semantic_reranker_only_runs_for_thorough_multi_source_results() -> None:
    llm = _NoCallLlm()
    reranker = ResultReranker(llm)
    results = [
        {"ok": True, "logical_source": "web"},
        {"ok": True, "logical_source": "github"},
    ]

    assert reranker.should_rerank("standard", results) is False
    assert reranker.should_rerank("thorough", results) is True
    assert reranker.should_rerank(
        "thorough",
        [{"ok": True, "logical_source": "web"}, {"ok": True, "logical_source": "web"}],
    ) is False
    assert llm.calls == 0
