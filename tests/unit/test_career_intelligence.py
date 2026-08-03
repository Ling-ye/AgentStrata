from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from chatcopilot.agent.tools.registry import build_tools_schema
from chatcopilot.botspec.registry import known_tool_pack_names, resolve_tool_modules
from chatcopilot.external_tools.career.dates import normalize_date
from chatcopilot.external_tools.career.models import (
    PROVIDER_DIRECT,
    PROVIDER_RESEARCH_FALLBACK,
    JobListing,
    ProviderResult,
)
from chatcopilot.external_tools.career.providers import get_providers
from chatcopilot.external_tools.career.service import CareerIntelService
from chatcopilot.external_tools.career.spec import _MAX_TOOL_RESULT_CHARS, _bounded_payload
from chatcopilot.external_tools.career.store import CareerIntelStore


def _job(*, requirements: str = "熟悉 Python 与 Agent") -> JobListing:
    published = (date.today() - timedelta(days=5)).isoformat()
    return JobListing(
        company="示例公司",
        source_job_id="position-1",
        title="Agent 后端工程师",
        location="示例城市",
        source_url="https://careers.example.org/jobs/position-1",
        responsibilities="建设 Agent 平台与检索服务",
        requirements=requirements,
        published_at=published,
        published_on=published,
        source_mode=PROVIDER_DIRECT,
    )


def _success(*jobs: JobListing, complete: bool = True) -> ProviderResult:
    return ProviderResult(
        provider="example-provider",
        company="示例公司",
        ok=True,
        jobs=jobs,
        source_url="https://careers.example.org/",
        mode=PROVIDER_DIRECT,
        snapshot_complete=complete,
        diagnostics={"snapshot_complete": complete},
    )


def _scan(store: CareerIntelStore, *jobs: JobListing, keywords=None, complete=True):
    return store.record_scan(
        started_at="2026-06-21T01:00:00+00:00",
        companies=["示例公司"],
        keywords=keywords or ["Agent", "Python"],
        locations=[],
        posted_within_days=30,
        results=[_success(*jobs, complete=complete)],
    )


def test_career_tool_pack_and_six_tools_are_registered() -> None:
    assert "career.intelligence" in known_tool_pack_names()
    assert resolve_tool_modules(("career.intelligence",)) == (
        "chatcopilot.external_tools.career.spec",
    )
    _, tools = build_tools_schema(tool_packs=["career.intelligence"])
    assert set(tools) == {
        "career_watchlist_update",
        "career_watchlist_show",
        "search_company_ai_jobs",
        "career_jobs_ingest",
        "career_intel_ingest",
        "career_intel_query",
    }


def test_public_registry_has_no_fixed_company_provider() -> None:
    assert get_providers() == ()


def test_search_requires_user_selected_company_and_returns_fallback() -> None:
    with TemporaryDirectory() as tmp:
        service = CareerIntelService(Path(tmp))
        with pytest.raises(ValueError, match="显式指定"):
            service.search_jobs()

        result = service.search_jobs(companies=["示例公司"], keywords=["Agent"])
        assert result["unknown_companies"] == ["示例公司"]
        assert result["providers"][0]["mode"] == PROVIDER_RESEARCH_FALLBACK
        assert "示例公司" in result["providers"][0]["fallback_query"]


def test_normalize_date_supports_chinese_iso_and_timestamp() -> None:
    assert normalize_date("2026年06月08日") == "2026-06-08"
    assert normalize_date("2026-06-08T10:00:00Z") == "2026-06-08"
    assert normalize_date(1780876800) == "2026-06-08"


def test_same_scope_tracks_changes_and_two_complete_misses() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        first = _scan(store, _job())
        changed = _scan(store, _job(requirements="熟悉 Go 与 Agent"))
        first_miss = _scan(store)
        second_miss = _scan(store)

        assert len(first["new_jobs"]) == 1
        assert len(changed["changed_jobs"]) == 1
        assert first_miss["suspected_closed_jobs"] == []
        assert len(second_miss["suspected_closed_jobs"]) == 1


def test_incomplete_scan_does_not_advance_missing_count() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        first = _scan(store, _job())
        _scan(store, complete=False)

        with store.connect() as db:
            state = db.execute(
                "SELECT missing_scans, status FROM job_scope_state WHERE scope_id=?",
                (first["providers"][0]["scope_id"],),
            ).fetchone()
        assert dict(state) == {"missing_scans": 0, "status": "active"}


def test_generic_fallback_job_is_ingested_but_never_marked_closed() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        record = {
            "company": "示例公司",
            "title": "Agent 后端工程师",
            "location": "示例城市",
            "source_url": "https://careers.example.org/jobs/position-2",
            "published_at": date.today().isoformat(),
        }
        scope = {
            "keywords": ["Agent", "后端"],
            "locations": [],
            "posted_within_days": 30,
            "source_name": "web_research",
        }
        second_record = {
            **record,
            "title": "平台工程师",
            "source_url": "https://careers.example.org/jobs/position-3",
        }
        first = store.ingest_jobs([record, second_record], scan_scope=scope)
        second = store.ingest_jobs([record], scan_scope=scope)

        assert len(first["new_jobs"]) == 2
        assert {item["source_mode"] for item in first["new_jobs"]} == {
            PROVIDER_RESEARCH_FALLBACK
        }
        assert second["suspected_closed_jobs"] == []


def test_fallback_job_rejects_community_url_and_old_date() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        scope = {"keywords": ["Agent"], "posted_within_days": 30, "source_name": "web"}
        bad = {
            "company": "示例公司",
            "title": "Agent 后端",
            "source_url": "https://www.nowcoder.com/discuss/1",
            "published_at": date.today().isoformat(),
        }
        with pytest.raises(ValueError, match="没有可写入"):
            store.ingest_jobs([bad], scan_scope=scope)

        old = {
            **bad,
            "source_url": "https://careers.example.org/jobs/old-position",
            "published_at": "2020-01-01",
        }
        with pytest.raises(ValueError, match="没有可写入"):
            store.ingest_jobs([old], scan_scope=scope)


def test_evidence_grade_is_downgraded_and_interview_fields_are_required() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        result = store.ingest_evidence(
            [
                {
                    "kind": "workplace",
                    "company": "示例公司",
                    "source_name": "社区帖子",
                    "source_url": "https://www.xiaohongshu.com/explore/1",
                    "source_type": "official",
                    "source_grade": "A",
                    "confidence": "low",
                    "published_at": date.today().isoformat(),
                    "payload": {},
                }
            ]
        )
        assert result["downgraded"] == 1
        assert store.query(kind="evidence")["evidence"][0]["source_grade"] == "C"

        with pytest.raises(ValueError, match="面试题证据缺少字段"):
            store.ingest_evidence(
                [
                    {
                        "kind": "interview_question",
                        "company": "示例公司",
                        "source_name": "完整面经",
                        "source_url": "https://example.org/interview",
                        "source_type": "complete_experience",
                        "source_grade": "B",
                        "confidence": "medium",
                        "published_at": "2026-06-01",
                        "payload": {},
                    }
                ]
            )


def test_tool_payload_is_valid_json_and_bounded() -> None:
    payload = {
        "jobs": [
            {"title": f"job-{index}", "requirements": "x" * 5000}
            for index in range(100)
        ]
    }
    bounded = _bounded_payload(payload)
    text = json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))
    assert len(text) <= _MAX_TOOL_RESULT_CHARS
    assert "truncated" in bounded


def test_workspace_databases_start_empty_and_are_isolated() -> None:
    with TemporaryDirectory() as left, TemporaryDirectory() as right:
        first = CareerIntelStore(Path(left))
        second = CareerIntelStore(Path(right))
        assert first.get_watchlist()["companies"] == []
        assert second.get_watchlist()["companies"] == []

        first.update_watchlist(
            companies=["示例公司"], keywords=None, locations=None, replace=True
        )
        assert first.get_watchlist()["companies"] == ["示例公司"]
        assert second.get_watchlist()["companies"] == []
