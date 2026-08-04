from __future__ import annotations

import json
import sqlite3
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
from chatcopilot.external_tools.career.providers.tencent import TencentProvider
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


def test_public_registry_exposes_supported_providers_without_default_watchlist() -> None:
    providers = get_providers()
    assert [(provider.id, provider.company) for provider in providers] == [
        ("tencent", "腾讯"),
        ("bytedance", "字节跳动"),
        ("minimax", "MiniMax"),
    ]
    for provider in providers[1:]:
        result = provider.search(
            keywords=("Agent",), locations=(), limit=10, posted_within_days=30
        )
        assert result.mode == PROVIDER_RESEARCH_FALLBACK
        assert result.fallback_query
        assert not result.snapshot_complete

    with TemporaryDirectory() as tmp:
        assert CareerIntelStore(Path(tmp)).get_watchlist()["companies"] == []


def test_tencent_provider_paginates_and_normalizes_dates() -> None:
    pages = {
        1: {
            "Data": {
                "Count": 2,
                "Posts": [
                    {
                        "PostId": "post-1",
                        "RecruitPostName": "Agent 后端工程师",
                        "LocationName": "深圳",
                        "LastUpdateTime": "2026年06月20日",
                    }
                ],
            }
        },
        2: {
            "Data": {
                "Count": 2,
                "Posts": [
                    {
                        "PostId": "post-2",
                        "RecruitPostName": "大模型平台工程师",
                        "LocationName": "上海",
                        "LastUpdateTime": "2026-06-19T10:00:00Z",
                    }
                ],
            }
        },
    }

    class FakeClient:
        def __init__(self) -> None:
            self.pages: list[int] = []

        def get_json(self, url, params):
            del url
            page = int(params["pageIndex"])
            self.pages.append(page)
            return pages[page]

    client = FakeClient()
    provider = TencentProvider(client=client)
    provider.page_size = 1
    result = provider.search(
        keywords=("Agent",), locations=(), limit=20, posted_within_days=3650
    )

    assert client.pages == [1, 2]
    assert result.snapshot_complete
    assert [job.published_on for job in result.jobs] == ["2026-06-20", "2026-06-19"]
    assert result.jobs[0].source_url.startswith("https://")


def test_tencent_provider_processes_all_keywords_without_silent_slice() -> None:
    class EmptyClient:
        def __init__(self) -> None:
            self.keywords: list[str] = []

        def get_json(self, url, params):
            del url
            self.keywords.append(params["keyword"])
            return {"Data": {"Count": 0, "Posts": []}}

    client = EmptyClient()
    keywords = tuple(f"keyword-{index}" for index in range(12))
    TencentProvider(client=client).search(
        keywords=keywords, locations=(), limit=20, posted_within_days=30
    )
    assert len(client.keywords) == len(keywords)
    assert set(client.keywords) == set(keywords)


def test_tencent_provider_failure_returns_research_fallback() -> None:
    class FailingClient:
        def get_json(self, url, params):
            del url, params
            raise TimeoutError("timeout")

    result = TencentProvider(client=FailingClient()).search(
        keywords=("Agent",), locations=(), limit=20, posted_within_days=30
    )
    assert not result.ok
    assert result.mode == PROVIDER_RESEARCH_FALLBACK
    assert result.fallback_query
    assert not result.snapshot_complete


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


def test_different_scope_and_incomplete_scan_do_not_advance_missing_count() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        first = _scan(store, _job())
        _scan(store, keywords=["Go"])
        _scan(store, complete=False)

        with store.connect() as db:
            state = db.execute(
                "SELECT missing_scans, status FROM job_scope_state WHERE scope_id=?",
                (first["providers"][0]["scope_id"],),
            ).fetchone()
        assert dict(state) == {"missing_scans": 0, "status": "active"}


def test_job_stays_globally_active_when_another_scope_still_sees_it() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        _scan(store, _job(), keywords=["Agent"])
        _scan(store, _job(), keywords=["Python"])
        _scan(store, keywords=["Agent"])
        _scan(store, keywords=["Agent"])

        assert store.query(kind="jobs")["jobs"][0]["status"] == "active"


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


@pytest.mark.parametrize(
    ("company", "source_url"),
    [
        ("腾讯", "https://example.org/jobs/position-1"),
        ("腾讯", "https://careers.tencent.com/jobdesc.html"),
        ("字节跳动", "https://jobs.bytedance.com/experienced/position"),
        ("MiniMax", "https://www.minimaxi.com/careers"),
    ],
)
def test_known_provider_fallback_rejects_non_detail_or_non_official_url(
    company: str,
    source_url: str,
) -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        record = {
            "company": company,
            "title": "Agent 后端工程师",
            "source_url": source_url,
            "published_at": date.today().isoformat(),
        }
        with pytest.raises(ValueError, match="没有可写入"):
            store.ingest_jobs(
                [record],
                scan_scope={"keywords": ["Agent"], "posted_within_days": 30},
            )


@pytest.mark.parametrize(
    ("company", "source_url"),
    [
        ("腾讯", "https://careers.tencent.com/jobdesc.html?postId=position-1"),
        ("字节跳动", "https://jobs.bytedance.com/experienced/position/123/detail"),
        ("MiniMax", "https://www.minimaxi.com/careers/position-1"),
        ("示例公司", "https://careers.example.org/jobs/position-1"),
    ],
)
def test_official_detail_urls_are_accepted(
    company: str,
    source_url: str,
) -> None:
    with TemporaryDirectory() as tmp:
        result = CareerIntelStore(Path(tmp)).ingest_jobs(
            [
                {
                    "company": company,
                    "title": "Agent 后端工程师",
                    "source_url": source_url,
                    "published_at": date.today().isoformat(),
                }
            ],
            scan_scope={"keywords": ["Agent"], "posted_within_days": 30},
        )
        assert len(result["new_jobs"]) == 1


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


def test_known_provider_official_evidence_requires_an_official_host() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        base = {
            "kind": "workplace",
            "company": "腾讯",
            "source_name": "官方招聘",
            "source_type": "official",
            "source_grade": "A",
            "confidence": "high",
            "published_at": date.today().isoformat(),
            "payload": {},
        }
        store.ingest_evidence(
            [
                {
                    **base,
                    "source_url": "https://example.org/tencent-recruiting",
                },
                {
                    **base,
                    "source_url": "https://careers.tencent.com/jobdesc.html?postId=1",
                },
            ]
        )

        evidence = store.query(kind="evidence")["evidence"]
        grades = {
            item["source_url"]: item["source_grade"]
            for item in evidence
        }
        assert grades["https://example.org/tencent-recruiting"] == "D"
        assert grades["https://careers.tencent.com/jobdesc.html?postId=1"] == "A"


def test_frequency_is_scoped_by_role_time_and_excludes_grade_d() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))

        def record(url: str, role: str, grade: str, published: str):
            return {
                "kind": "interview_question",
                "company": "示例公司",
                "role_family": role,
                "topic": "RAG 召回与重排",
                "normalized_key": "rag-retrieval-rerank",
                "source_name": "面经",
                "source_url": url,
                "source_type": (
                    "complete_experience" if grade != "D" else "search_snippet"
                ),
                "source_grade": grade,
                "published_at": published,
                "confidence": "medium",
                "payload": {"question": "如何设计 RAG 召回与重排？"},
            }

        store.ingest_evidence(
            [
                record(
                    "https://example.org/1",
                    "Agent 后端",
                    "B",
                    (date.today() - timedelta(days=30)).isoformat(),
                ),
                {
                    **record(
                        "https://example.org/2",
                        "Agent 后端",
                        "B",
                        date.today().isoformat(),
                    ),
                    "topic": "检索与重排",
                },
                record("https://example.org/3", "算法", "B", date.today().isoformat()),
                record(
                    "https://example.org/4",
                    "Agent 后端",
                    "D",
                    date.today().isoformat(),
                ),
                record("https://example.org/5", "Agent 后端", "B", "2020-01-01"),
            ]
        )
        frequent = store.query(kind="evidence")["frequent_interview_questions"]
        assert len(frequent) == 1
        assert frequent[0]["role_family"] == "Agent 后端"
        assert frequent[0]["source_count"] == 2


def test_salary_requires_components_and_returns_sample_context() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        base = {
            "kind": "salary",
            "company": "示例公司",
            "role_family": "Agent 后端",
            "source_name": "候选人自述",
            "source_url": "https://example.org/salary/1",
            "source_type": "complete_experience",
            "source_grade": "B",
            "published_at": date.today().isoformat(),
            "confidence": "medium",
        }
        with pytest.raises(ValueError, match="组成项"):
            store.ingest_evidence([{**base, "payload": {"total_comp": "100w"}}])
        store.ingest_evidence(
            [
                {
                    **base,
                    "payload": {
                        "total_comp": "100w",
                        "monthly_salary": "50k",
                        "location": "示例城市",
                        "level": "资深",
                    },
                }
            ]
        )
        store.ingest_evidence(
            [
                {
                    **base,
                    "source_url": "https://example.org/salary/search-only",
                    "source_type": "search_snippet",
                    "source_grade": "D",
                    "payload": {
                        "monthly_salary": "60k",
                        "location": "示例城市",
                        "level": "资深",
                    },
                }
            ]
        )
        samples = store.query(kind="evidence")["salary_samples"]
        assert samples[0]["sample_count"] == 1
        assert samples[0]["location"] == "示例城市"


def test_query_filters_recent_records_and_compacts_job_details() -> None:
    with TemporaryDirectory() as tmp:
        store = CareerIntelStore(Path(tmp))
        _scan(store, _job())
        result = store.query(kind="jobs", since_days=30, detail=False)
        assert len(result["jobs"]) == 1
        assert "responsibilities" not in result["jobs"][0]
        assert store.query(kind="jobs", since_days=1)["jobs"] == []


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


def test_v1_database_migrates_to_v2_without_losing_records() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "career_intelligence"
        root.mkdir()
        path = root / "intelligence.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(
            """
            CREATE TABLE metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO metadata VALUES('schema_version', '1');
            CREATE TABLE companies(
                canonical_name TEXT PRIMARY KEY,
                aliases_json TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE jobs(
                identity TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                source_job_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL,
                responsibilities TEXT NOT NULL DEFAULT '',
                requirements TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                relevance_score INTEGER NOT NULL DEFAULT 0,
                relevance_reasons_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                missing_scans INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
            );
            INSERT INTO jobs VALUES(
                '示例公司:1','示例公司','1','Agent 后端','示例城市',
                'https://careers.example.org/jobs/1','','','2026年06月01日',
                'hash',80,'[]','2026-06-01','2026-06-01',0,'active'
            );
            CREATE TABLE job_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(identity, content_hash)
            );
            CREATE TABLE evidence(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                company TEXT NOT NULL,
                role_family TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                normalized_key TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_grade TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT '',
                captured_at TEXT NOT NULL,
                confidence TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(kind, company, normalized_key, source_url)
            );
            INSERT INTO evidence(
                kind,company,role_family,topic,normalized_key,source_name,
                source_url,source_grade,published_at,captured_at,confidence,payload_json
            ) VALUES(
                'workplace','示例公司','','','','社区',
                'https://www.xiaohongshu.com/explore/legacy','A',
                '2026-06-01','2026-06-01','low','{}'
            );
            CREATE TABLE scans(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                companies_json TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                provider_status_json TEXT NOT NULL,
                new_count INTEGER NOT NULL,
                changed_count INTEGER NOT NULL,
                suspect_count INTEGER NOT NULL
            );
            """
        )
        db.commit()
        db.close()

        store = CareerIntelStore(Path(tmp))
        with store.connect() as migrated:
            version = migrated.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()[0]
            job = migrated.execute(
                "SELECT published_on, source_mode FROM jobs WHERE identity='示例公司:1'"
            ).fetchone()
            evidence_grade = migrated.execute(
                "SELECT source_grade FROM evidence WHERE source_url LIKE '%legacy'"
            ).fetchone()[0]
        assert version == "2"
        assert tuple(job) == ("2026-06-01", PROVIDER_DIRECT)
        assert evidence_grade == "C"


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
