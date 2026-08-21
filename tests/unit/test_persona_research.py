from __future__ import annotations

import json

from chatcopilot.agent.persona.draft_agent import PersonaDraftAgent
from chatcopilot.core.llm_client import ChatResult


class _Coordinator:
    def __init__(self, payload=None, *, fail=False):
        self.payload = payload or {}
        self.fail = fail
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("search offline")
        return self.payload


class _Llm:
    model = "research-persona-test"

    def __init__(self, results=None, *, fail=None):
        self.results = list(results or [])
        self.fail = fail
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return self.results.pop(0)


def _tool_call(query="异世界情绪 官方 身份"):
    return ChatResult(
        tool_calls=[
            {
                "id": "search-1",
                "type": "function",
                "function": {
                    "name": "search_information",
                    "arguments": json.dumps(
                        {"query": query, "objective": "核实人物身份与表达风格"},
                        ensure_ascii=False,
                    ),
                },
            }
        ],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 10, "total_tokens": 10},
    )


def _final(*, urls=()):
    return ChatResult(
        content=json.dumps(
            {
                "markdown": "# 人格\n\n## 身份与表达\n以目标角色身份使用中文自然交流。",
                "source_urls": list(urls),
            },
            ensure_ascii=False,
        ),
        finish_reason="stop",
        usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
    )


def _payload():
    return {
        "results": [
            {
                "items": [
                    {
                        "url": "https://official.example/profile?lang=zh#top",
                        "title": "官方简介",
                        "content": "人物公开介绍",
                    },
                    {
                        "url": "https://wiki.example/entry",
                        "title": "公开资料",
                        "summary": "背景和表达资料",
                    },
                ]
            }
        ]
    }


def test_research_agent_chooses_query_and_authors_the_complete_document() -> None:
    urls = ("https://official.example/profile", "https://wiki.example/entry")
    llm = _Llm([_tool_call(), _final(urls=urls)])
    coordinator = _Coordinator(_payload())

    result = PersonaDraftAgent(llm=llm, coordinator=coordinator).draft(
        owner_requirement="模仿异世界情绪，始终使用中文回复",
        operation="research",
        research_required=True,
    )

    assert result.ok is True
    assert result.markdown.startswith("# 人格")
    assert result.source_urls == urls
    assert result.search_calls == 1
    assert len(result.calls) == 2
    assert len(coordinator.requests) == 1
    assert "异世界情绪" in coordinator.requests[0].objective
    assert llm.calls[0]["max_retries"] == 0


def test_abstract_persona_can_be_drafted_without_search() -> None:
    llm = _Llm([_final()])
    coordinator = _Coordinator(_payload())

    result = PersonaDraftAgent(llm=llm, coordinator=coordinator).draft(
        owner_requirement="说话更简洁温柔",
        operation="set",
        research_required=False,
    )

    assert result.ok is True
    assert result.search_calls == 0
    assert coordinator.requests == []


def test_append_gives_current_persona_to_agent_but_returns_one_full_replacement() -> None:
    llm = _Llm([_final()])
    result = PersonaDraftAgent(llm=llm, coordinator=None).draft(
        owner_requirement="再活泼一点",
        operation="append",
        current_persona="# 旧人格\n保持简洁",
    )

    assert result.ok is True
    request = json.loads(llm.calls[0]["messages"][1]["content"])
    assert request["operation"] == "append"
    assert request["current_persona"] == "# 旧人格\n保持简洁"


def test_invented_or_insufficient_sources_are_rejected() -> None:
    invented = PersonaDraftAgent(
        llm=_Llm([_tool_call(), _final(urls=("https://invented.example/x",))]),
        coordinator=_Coordinator(_payload()),
    ).draft(
        owner_requirement="模仿某角色",
        operation="research",
        research_required=True,
    )
    assert invented.error_code == "persona_source_not_observed"

    insufficient = PersonaDraftAgent(
        llm=_Llm(
            [_tool_call(), _final(urls=("https://official.example/profile",))]
        ),
        coordinator=_Coordinator(_payload()),
    ).draft(
        owner_requirement="模仿某角色",
        operation="research",
        research_required=True,
    )
    assert insufficient.error_code == "persona_sources_insufficient"


def test_search_and_provider_failures_preserve_stable_diagnostics() -> None:
    search = PersonaDraftAgent(
        llm=_Llm([_tool_call()]), coordinator=_Coordinator(fail=True)
    ).draft(
        owner_requirement="模仿某角色",
        operation="research",
        research_required=True,
    )
    assert search.error_code == "persona_search_failed"
    assert search.error_kind == "RuntimeError"

    provider = PersonaDraftAgent(
        llm=_Llm(fail=TimeoutError("provider timeout")), coordinator=None
    ).draft(owner_requirement="更简洁", operation="set")
    assert provider.error_code == "persona_draft_timeout"
    assert provider.error_kind == "TimeoutError"
    assert len(provider.calls) == 1
    assert provider.calls[0].ok is False


def test_invalid_json_and_missing_research_provider_fail_closed() -> None:
    invalid = PersonaDraftAgent(
        llm=_Llm([ChatResult(content="# 不是 JSON")]), coordinator=None
    ).draft(owner_requirement="更简洁", operation="set")
    assert invalid.error_code == "persona_draft_invalid"

    unavailable = PersonaDraftAgent(llm=_Llm(), coordinator=None).draft(
        owner_requirement="模仿某角色",
        operation="research",
        research_required=True,
    )
    assert unavailable.error_code == "persona_search_unavailable"
    assert unavailable.calls == ()
