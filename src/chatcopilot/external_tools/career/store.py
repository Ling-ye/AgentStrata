"""SQLite persistence for workspace-local career intelligence."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse

from chatcopilot.external_tools.career.dates import cutoff_date, is_recent, normalize_date
from chatcopilot.external_tools.career.models import (
    PROVIDER_DIRECT,
    PROVIDER_RESEARCH_FALLBACK,
    JobListing,
    ProviderResult,
)
from chatcopilot.external_tools.career.providers.registry import (
    find_provider,
    supported_company_aliases,
)
from chatcopilot.external_tools.career.quality import (
    effective_source_grade,
    validate_and_normalize_evidence,
)
from chatcopilot.external_tools.career.scope import build_scope

_SCHEMA_VERSION = 2

DEFAULT_WATCHLIST = {
    "companies": [],
    "keywords": [
        "Agent", "AI Agent", "智能体", "LLM", "大模型", "RAG", "MCP",
        "Python", "Go", "后端开发", "模型应用", "Agent 平台", "推理平台", "AI Infra",
    ],
    "locations": [],
}

DEFAULT_COMPANIES = supported_company_aliases()
_NON_JOB_HOSTS = (
    "bing.com", "google.com", "baidu.com", "xiaohongshu.com", "zhihu.com",
    "nowcoder.com", "maimai.cn",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CareerIntelStore:
    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root / "career_intelligence"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "intelligence.sqlite3"
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            self._create_tables(db)
            version_row = db.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if version_row:
                version = int(version_row["value"])
            else:
                job_columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
                version = _SCHEMA_VERSION if "published_on" in job_columns else 1
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"career intelligence schema v{version} newer than supported v{_SCHEMA_VERSION}"
                )
            if version < _SCHEMA_VERSION:
                self._migrate(db, version)
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            if db.execute("SELECT 1 FROM metadata WHERE key='watchlist'").fetchone() is None:
                db.execute(
                    "INSERT INTO metadata(key, value) VALUES('watchlist', ?)",
                    (json.dumps(DEFAULT_WATCHLIST, ensure_ascii=False),),
                )
            now = utc_now()
            for company, aliases in DEFAULT_COMPANIES.items():
                db.execute(
                    """INSERT OR IGNORE INTO companies(canonical_name, aliases_json, enabled, updated_at)
                       VALUES(?,?,1,?)""",
                    (company, json.dumps(aliases, ensure_ascii=False), now),
                )
            self._create_indexes(db)

    @staticmethod
    def _create_tables(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS companies (
                canonical_name TEXT PRIMARY KEY,
                aliases_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                identity TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                source_job_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL,
                responsibilities TEXT NOT NULL DEFAULT '',
                requirements TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                published_on TEXT NOT NULL DEFAULT '',
                source_mode TEXT NOT NULL DEFAULT 'direct',
                content_hash TEXT NOT NULL,
                relevance_score INTEGER NOT NULL DEFAULT 0,
                relevance_reasons_json TEXT NOT NULL DEFAULT '[]',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                missing_scans INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS job_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identity TEXT NOT NULL REFERENCES jobs(identity) ON DELETE CASCADE,
                content_hash TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(identity, content_hash)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                company TEXT NOT NULL,
                role_family TEXT NOT NULL DEFAULT '',
                topic TEXT NOT NULL DEFAULT '',
                normalized_key TEXT NOT NULL DEFAULT '',
                source_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'search_snippet',
                source_grade TEXT NOT NULL,
                published_at TEXT NOT NULL DEFAULT '',
                published_on TEXT NOT NULL DEFAULT '',
                captured_at TEXT NOT NULL,
                confidence TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(kind, company, normalized_key, source_url)
            );
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                companies_json TEXT NOT NULL,
                keywords_json TEXT NOT NULL,
                scope_json TEXT NOT NULL DEFAULT '[]',
                provider_status_json TEXT NOT NULL,
                new_count INTEGER NOT NULL,
                changed_count INTEGER NOT NULL,
                suspect_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS job_scope_state (
                scope_id TEXT NOT NULL,
                identity TEXT NOT NULL REFERENCES jobs(identity) ON DELETE CASCADE,
                company TEXT NOT NULL,
                missing_scans INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(scope_id, identity)
            );
            CREATE TABLE IF NOT EXISTS scan_jobs (
                scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                scope_id TEXT NOT NULL,
                identity TEXT NOT NULL REFERENCES jobs(identity) ON DELETE CASCADE,
                PRIMARY KEY(scan_id, scope_id, identity)
            );
            """
        )

    def _migrate(self, db: sqlite3.Connection, version: int) -> None:
        if version < 2:
            self._ensure_column(db, "jobs", "published_on", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "jobs", "source_mode", "TEXT NOT NULL DEFAULT 'direct'")
            self._ensure_column(
                db, "evidence", "source_type", "TEXT NOT NULL DEFAULT 'search_snippet'"
            )
            self._ensure_column(db, "evidence", "published_on", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "scans", "scope_json", "TEXT NOT NULL DEFAULT '[]'")
            for row in db.execute("SELECT identity, published_at FROM jobs").fetchall():
                db.execute(
                    "UPDATE jobs SET published_on=? WHERE identity=?",
                    (normalize_date(row["published_at"]), row["identity"]),
                )
            grade_to_type = {"A": "official", "B": "complete_experience", "C": "community_post"}
            for row in db.execute(
                "SELECT id, source_url, source_grade, published_at FROM evidence"
            ).fetchall():
                source_type = grade_to_type.get(row["source_grade"], "search_snippet")
                source_grade = effective_source_grade(
                    {
                        "source_url": row["source_url"],
                        "source_type": source_type,
                        "source_grade": row["source_grade"],
                    }
                )
                db.execute(
                    "UPDATE evidence SET source_type=?, source_grade=?, published_on=? WHERE id=?",
                    (
                        source_type,
                        source_grade,
                        normalize_date(row["published_at"]),
                        row["id"],
                    ),
                )

    @staticmethod
    def _ensure_column(
        db: sqlite3.Connection, table: str, column: str, declaration: str
    ) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @staticmethod
    def _create_indexes(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_company_date
                ON jobs(company, published_on, last_seen_at);
            CREATE INDEX IF NOT EXISTS idx_evidence_company_date
                ON evidence(company, kind, published_on);
            CREATE INDEX IF NOT EXISTS idx_scope_state
                ON job_scope_state(scope_id, status, last_seen_at);
            """
        )

    def get_watchlist(self) -> dict[str, list[str]]:
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='watchlist'").fetchone()
        return json.loads(row["value"]) if row else dict(DEFAULT_WATCHLIST)

    def update_watchlist(
        self,
        *,
        companies: list[str] | None,
        keywords: list[str] | None,
        locations: list[str] | None,
        replace: bool,
    ) -> dict[str, list[str]]:
        current = self.get_watchlist()
        updates = {"companies": companies, "keywords": keywords, "locations": locations}
        for key, values in updates.items():
            if values is None:
                continue
            cleaned = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
            if key == "companies":
                cleaned = list(dict.fromkeys(canonical_company(value) for value in cleaned))
            current[key] = cleaned if replace else list(dict.fromkeys(current.get(key, []) + cleaned))
        if not current["companies"] or not current["keywords"]:
            raise ValueError("关注公司和关键词均不能为空")
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('watchlist', ?)",
                (json.dumps(current, ensure_ascii=False),),
            )
            now = utc_now()
            for company in current["companies"]:
                aliases = DEFAULT_COMPANIES.get(company, [company])
                db.execute(
                    """INSERT INTO companies(canonical_name, aliases_json, enabled, updated_at)
                       VALUES(?,?,1,?) ON CONFLICT(canonical_name) DO UPDATE SET
                       aliases_json=excluded.aliases_json, enabled=1, updated_at=excluded.updated_at""",
                    (company, json.dumps(aliases, ensure_ascii=False), now),
                )
        return current

    def record_scan(
        self,
        *,
        started_at: str,
        companies: list[str],
        keywords: list[str],
        locations: list[str] | None = None,
        posted_within_days: int = 30,
        results: Iterable[ProviderResult],
    ) -> dict[str, Any]:
        completed_at = utc_now()
        new_jobs: list[dict[str, Any]] = []
        changed_jobs: list[dict[str, Any]] = []
        suspect_jobs: list[dict[str, Any]] = []
        statuses: list[dict[str, Any]] = []
        memberships: list[tuple[str, str]] = []
        scopes: list[dict[str, Any]] = []
        with self.connect() as db:
            for result in results:
                scope = build_scope(
                    company=result.company,
                    provider=result.provider,
                    source_mode=result.mode,
                    keywords=keywords,
                    locations=locations or [],
                    posted_within_days=posted_within_days,
                )
                scopes.append(scope)
                complete = bool(result.snapshot_complete)
                statuses.append(
                    {
                        "provider": result.provider,
                        "company": result.company,
                        "ok": result.ok,
                        "mode": result.mode,
                        "snapshot_complete": complete,
                        "job_count": len(result.jobs),
                        "error": result.error,
                        "fallback_query": result.fallback_query,
                        "source_url": result.source_url,
                        "scope_id": scope["scope_id"],
                        "diagnostics": result.diagnostics,
                    }
                )
                if not result.ok:
                    continue
                seen: set[str] = set()
                for job in result.jobs:
                    seen.add(job.identity)
                    memberships.append((scope["scope_id"], job.identity))
                    existing = db.execute(
                        "SELECT content_hash FROM jobs WHERE identity=?", (job.identity,)
                    ).fetchone()
                    score, reasons = relevance(job, keywords)
                    payload = job.as_dict()
                    published_on = job.published_on or normalize_date(job.published_at)
                    if existing is None:
                        db.execute(
                            """INSERT INTO jobs(
                                identity, company, source_job_id, title, location, source_url,
                                responsibilities, requirements, published_at, published_on,
                                source_mode, content_hash, relevance_score, relevance_reasons_json,
                                first_seen_at, last_seen_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                job.identity, job.company, job.source_job_id, job.title, job.location,
                                job.source_url, job.responsibilities, job.requirements, job.published_at,
                                published_on, job.source_mode, job.content_hash, score,
                                json.dumps(reasons, ensure_ascii=False), completed_at, completed_at,
                            ),
                        )
                        new_jobs.append(_job_summary(job, score, reasons, change="new"))
                    else:
                        changed = existing["content_hash"] != job.content_hash
                        db.execute(
                            """UPDATE jobs SET title=?, location=?, source_url=?, responsibilities=?,
                                requirements=?, published_at=?, published_on=?, source_mode=?,
                                content_hash=?, relevance_score=?, relevance_reasons_json=?,
                                last_seen_at=?, missing_scans=0, status='active' WHERE identity=?""",
                            (
                                job.title, job.location, job.source_url, job.responsibilities,
                                job.requirements, job.published_at, published_on, job.source_mode,
                                job.content_hash, score, json.dumps(reasons, ensure_ascii=False),
                                completed_at, job.identity,
                            ),
                        )
                        if changed:
                            changed_jobs.append(_job_summary(job, score, reasons, change="changed"))
                    db.execute(
                        """INSERT OR IGNORE INTO job_versions(identity, content_hash, captured_at, payload_json)
                           VALUES(?,?,?,?)""",
                        (job.identity, job.content_hash, completed_at, json.dumps(payload, ensure_ascii=False)),
                    )
                    db.execute(
                        """INSERT INTO job_scope_state(scope_id, identity, company, missing_scans, status, last_seen_at)
                           VALUES(?,?,?,0,'active',?) ON CONFLICT(scope_id, identity) DO UPDATE SET
                           missing_scans=0, status='active', last_seen_at=excluded.last_seen_at""",
                        (scope["scope_id"], job.identity, job.company, completed_at),
                    )

                if not (complete and result.mode == PROVIDER_DIRECT):
                    continue
                previous = db.execute(
                    """SELECT s.identity, s.missing_scans, j.title, j.location, j.source_url
                       FROM job_scope_state s JOIN jobs j ON j.identity=s.identity
                       WHERE s.scope_id=?""",
                    (scope["scope_id"],),
                ).fetchall()
                for row in previous:
                    if row["identity"] in seen:
                        continue
                    missed = int(row["missing_scans"]) + 1
                    status = "suspected_closed" if missed >= 2 else "not_seen"
                    db.execute(
                        "UPDATE job_scope_state SET missing_scans=?, status=? WHERE scope_id=? AND identity=?",
                        (missed, status, scope["scope_id"], row["identity"]),
                    )
                    _refresh_job_status(db, row["identity"])
                    if status == "suspected_closed":
                        suspect_jobs.append({**dict(row), "scope_id": scope["scope_id"]})

            cursor = db.execute(
                """INSERT INTO scans(started_at, completed_at, companies_json, keywords_json,
                   scope_json, provider_status_json, new_count, changed_count, suspect_count)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    started_at, completed_at, json.dumps(companies, ensure_ascii=False),
                    json.dumps(keywords, ensure_ascii=False), json.dumps(scopes, ensure_ascii=False),
                    json.dumps(statuses, ensure_ascii=False), len(new_jobs), len(changed_jobs),
                    len(suspect_jobs),
                ),
            )
            scan_id = int(cursor.lastrowid)
            db.executemany(
                "INSERT OR IGNORE INTO scan_jobs(scan_id, scope_id, identity) VALUES(?,?,?)",
                ((scan_id, scope_id, identity) for scope_id, identity in memberships),
            )
        return {
            "scan_id": scan_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "new_jobs": new_jobs,
            "changed_jobs": changed_jobs,
            "suspected_closed_jobs": suspect_jobs,
            "providers": statuses,
        }

    def ingest_jobs(
        self,
        records: list[dict[str, Any]],
        *,
        scan_scope: dict[str, Any],
    ) -> dict[str, Any]:
        keywords = _string_list(scan_scope.get("keywords"))
        locations = _string_list(scan_scope.get("locations"))
        posted_within_days = int(scan_scope.get("posted_within_days") or 30)
        if not keywords:
            raise ValueError("scan_scope.keywords 不能为空")
        if posted_within_days < 1 or posted_within_days > 3650:
            raise ValueError("scan_scope.posted_within_days 必须在 1 到 3650 之间")
        started_at = str(scan_scope.get("started_at") or utc_now())
        grouped: dict[str, list[JobListing]] = {}
        rejected: list[dict[str, str]] = []
        for index, record in enumerate(records):
            try:
                job = normalize_research_job(record, posted_within_days=posted_within_days)
            except ValueError as exc:
                rejected.append({"index": str(index), "error": str(exc)})
                continue
            grouped.setdefault(job.company, []).append(job)
        if not grouped:
            raise ValueError("没有可写入的官方岗位记录: " + json.dumps(rejected, ensure_ascii=False))
        results = [
            ProviderResult(
                provider=str(scan_scope.get("source_name") or "search_information"),
                company=company,
                ok=True,
                jobs=tuple(jobs),
                source_url=jobs[0].source_url,
                mode=PROVIDER_RESEARCH_FALLBACK,
                snapshot_complete=False,
                diagnostics={"fallback_ingest": True, "rejected_count": len(rejected)},
            )
            for company, jobs in grouped.items()
        ]
        report = self.record_scan(
            started_at=started_at,
            companies=list(grouped),
            keywords=keywords,
            locations=locations,
            posted_within_days=posted_within_days,
            results=results,
        )
        report["rejected"] = rejected
        report["note"] = "research_fallback 快照不完整，未再次发现不会推进疑似下线状态。"
        return report

    def ingest_evidence(self, records: list[dict[str, Any]]) -> dict[str, int]:
        inserted = 0
        updated = 0
        downgraded = 0
        now = utc_now()
        with self.connect() as db:
            for raw in records:
                record = validate_and_normalize_evidence(raw)
                downgraded += int(record["source_grade"] != str(raw.get("source_grade", "")).upper())
                key = str(record.get("normalized_key") or record.get("topic") or "").strip().casefold()
                values = (
                    str(record["kind"]), canonical_company(str(record["company"])),
                    str(record.get("role_family") or ""), str(record.get("topic") or ""), key,
                    str(record["source_name"]), str(record["source_url"]), str(record["source_type"]),
                    str(record["source_grade"]), str(record.get("published_at") or ""),
                    str(record.get("published_on") or ""), now, str(record["confidence"]),
                    json.dumps(record.get("payload") or {}, ensure_ascii=False),
                )
                exists = db.execute(
                    "SELECT id FROM evidence WHERE kind=? AND company=? AND normalized_key=? AND source_url=?",
                    (values[0], values[1], values[4], values[6]),
                ).fetchone()
                db.execute(
                    """INSERT INTO evidence(kind, company, role_family, topic, normalized_key,
                       source_name, source_url, source_type, source_grade, published_at,
                       published_on, captured_at, confidence, payload_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(kind, company, normalized_key, source_url) DO UPDATE SET
                       role_family=excluded.role_family, topic=excluded.topic,
                       source_name=excluded.source_name, source_type=excluded.source_type,
                       source_grade=excluded.source_grade, published_at=excluded.published_at,
                       published_on=excluded.published_on, captured_at=excluded.captured_at,
                       confidence=excluded.confidence, payload_json=excluded.payload_json""",
                    values,
                )
                updated += int(exists is not None)
                inserted += int(exists is None)
        return {"inserted": inserted, "updated": updated, "downgraded": downgraded}

    def query(
        self,
        *,
        companies: list[str] | None = None,
        kind: str = "all",
        limit: int = 50,
        since_days: int = 365,
        role_family: str = "",
        detail: bool = False,
    ) -> dict[str, Any]:
        if since_days < 0 or since_days > 3650:
            raise ValueError("since_days 必须在 0 到 3650 之间")
        company_values = [canonical_company(value) for value in companies or []]
        cutoff = cutoff_date(since_days)
        with self.connect() as db:
            jobs = []
            evidence = []
            if kind in ("all", "jobs"):
                where, params = _filters(
                    company_values, "published_on>=?", extra_params=[cutoff]
                )
                rows = db.execute(
                    f"SELECT * FROM jobs{where} ORDER BY relevance_score DESC, published_on DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
                jobs = [_decode_job(row, detail=detail) for row in rows]
            if kind in ("all", "evidence"):
                extra = ["published_on>=?"]
                params_extra: list[Any] = [cutoff]
                if role_family:
                    extra.append("role_family=?")
                    params_extra.append(role_family)
                where, params = _filters(company_values, *extra, extra_params=params_extra)
                rows = db.execute(
                    f"SELECT * FROM evidence{where} ORDER BY published_on DESC, captured_at DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
                evidence = [_decode_evidence(row, detail=detail) for row in rows]

            frequent_cutoff = cutoff_date(365)
            frequent_where, frequent_params = _filters(
                company_values,
                "kind='interview_question'",
                "normalized_key<>''",
                "source_grade<>'D'",
                "published_on>=?",
                *(('role_family=?',) if role_family else ()),
                extra_params=[frequent_cutoff, *([role_family] if role_family else [])],
            )
            frequent = db.execute(
                f"""SELECT company, role_family, normalized_key, MIN(topic) AS topic,
                       COUNT(DISTINCT source_url) AS source_count,
                       MIN(published_on) AS earliest_source,
                       MAX(published_on) AS latest_source
                   FROM evidence{frequent_where}
                   GROUP BY company, role_family, normalized_key
                   HAVING source_count>=2 ORDER BY source_count DESC, latest_source DESC LIMIT ?""",
                (*frequent_params, limit),
            ).fetchall()
            salary_where, salary_params = _filters(
                company_values,
                "kind='salary'",
                "source_grade<>'D'",
                "published_on>=?",
                *(('role_family=?',) if role_family else ()),
                extra_params=[cutoff, *([role_family] if role_family else [])],
            )
            salary_rows = db.execute(
                f"SELECT company, role_family, published_on, payload_json FROM evidence{salary_where}",
                salary_params,
            ).fetchall()
            last_scan = db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "filters": {"companies": company_values, "since_days": since_days, "role_family": role_family},
            "jobs": jobs,
            "evidence": evidence,
            "salary_samples": _salary_samples(salary_rows),
            "frequent_interview_questions": [dict(row) for row in frequent],
            "last_scan": _decode_scan(last_scan) if last_scan else None,
        }


def normalize_research_job(record: dict[str, Any], *, posted_within_days: int) -> JobListing:
    required = ("company", "title", "source_url", "published_at")
    missing = [key for key in required if not str(record.get(key) or "").strip()]
    if missing:
        raise ValueError("岗位缺少字段: " + ", ".join(missing))
    company = canonical_company(str(record["company"]))
    source_url = str(record["source_url"]).strip()
    _validate_official_job_url(company, source_url)
    published_on = normalize_date(record["published_at"])
    if not published_on:
        raise ValueError("published_at 必须是可解析日期")
    if not is_recent(published_on, posted_within_days):
        raise ValueError(f"岗位发布时间早于 {posted_within_days} 天窗口")
    return JobListing(
        company=company,
        title=str(record["title"]).strip(),
        location=str(record.get("location") or "").strip(),
        source_url=source_url,
        source_job_id=str(record.get("source_job_id") or "").strip(),
        responsibilities=str(record.get("responsibilities") or "").strip(),
        requirements=str(record.get("requirements") or "").strip(),
        published_at=str(record["published_at"]).strip(),
        published_on=published_on,
        source="official_search",
        source_mode=PROVIDER_RESEARCH_FALLBACK,
    )


def _validate_official_job_url(company: str, source_url: str) -> None:
    parsed = urlparse(source_url)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("source_url 必须是公开 HTTP(S) 岗位链接")
    if any(host == suffix or host.endswith("." + suffix) for suffix in _NON_JOB_HOSTS):
        raise ValueError("source_url 不能是搜索页、社区帖子或面经链接")
    provider = find_provider(company)
    if provider is not None:
        provider.validate_job_url(source_url)


def relevance(job: JobListing, keywords: list[str]) -> tuple[int, list[str]]:
    haystack = "\n".join((job.title, job.responsibilities, job.requirements)).casefold()
    matched = [keyword for keyword in keywords if keyword.casefold() in haystack]
    title_matches = [keyword for keyword in matched if keyword.casefold() in job.title.casefold()]
    score = len(matched) * 5 + len(title_matches) * 10
    score += sum(10 for marker in ("后端", "架构", "平台", "工程师", "研发") if marker in job.title)
    if "产品经理" in job.title:
        score -= 25
    return max(0, min(100, score)), matched[:12]


def canonical_company(value: str) -> str:
    provider = find_provider(value)
    if provider is not None:
        return provider.company
    return value.strip()


def _job_summary(
    job: JobListing, score: int, reasons: list[str], *, change: str
) -> dict[str, Any]:
    return {
        "identity": job.identity,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "published_on": job.published_on or normalize_date(job.published_at),
        "source_url": job.source_url,
        "source_mode": job.source_mode,
        "relevance_score": score,
        "relevance_reasons": reasons,
        "change": change,
    }


def _decode_job(row: sqlite3.Row, *, detail: bool) -> dict[str, Any]:
    data = dict(row)
    data["relevance_reasons"] = json.loads(data.pop("relevance_reasons_json"))
    if not detail:
        data.pop("responsibilities", None)
        data.pop("requirements", None)
        data.pop("content_hash", None)
    return data


def _decode_evidence(row: sqlite3.Row, *, detail: bool) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    if not detail and isinstance(data["payload"], dict):
        data["payload"].pop("raw_excerpt", None)
    return data


def _decode_scan(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("companies_json", "keywords_json", "scope_json", "provider_status_json"):
        data[key.removesuffix("_json")] = json.loads(data.pop(key))
    return data


def _filters(
    companies: list[str],
    *conditions: str,
    extra_params: list[Any] | None = None,
) -> tuple[str, list[Any]]:
    where = list(conditions)
    params: list[Any] = list(extra_params or [])
    if companies:
        where.insert(0, "company IN (%s)" % ",".join("?" for _ in companies))
        params = [*companies, *params]
    return ((" WHERE " + " AND ".join(where)) if where else "", params)


def _salary_samples(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        key = (
            row["company"], row["role_family"], str(payload.get("location") or ""),
            str(payload.get("level") or ""),
        )
        bucket = buckets.setdefault(
            key,
            {
                "company": key[0], "role_family": key[1], "location": key[2],
                "level": key[3], "sample_count": 0, "earliest_source": row["published_on"],
                "latest_source": row["published_on"],
            },
        )
        bucket["sample_count"] += 1
        bucket["earliest_source"] = min(bucket["earliest_source"], row["published_on"])
        bucket["latest_source"] = max(bucket["latest_source"], row["published_on"])
    return sorted(buckets.values(), key=lambda item: (-item["sample_count"], item["company"]))


def _refresh_job_status(db: sqlite3.Connection, identity: str) -> None:
    states = db.execute(
        "SELECT missing_scans, status FROM job_scope_state WHERE identity=?",
        (identity,),
    ).fetchall()
    if any(row["status"] == "active" for row in states):
        status, missed = "active", 0
    elif states and all(row["status"] == "suspected_closed" for row in states):
        status = "suspected_closed"
        missed = max(int(row["missing_scans"]) for row in states)
    else:
        status = "not_seen"
        missed = max((int(row["missing_scans"]) for row in states), default=0)
    db.execute(
        "UPDATE jobs SET missing_scans=?, status=? WHERE identity=?",
        (missed, status, identity),
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("scan_scope 中的 keywords/locations 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


__all__ = [
    "CareerIntelStore",
    "DEFAULT_COMPANIES",
    "DEFAULT_WATCHLIST",
    "canonical_company",
    "normalize_research_job",
    "relevance",
    "utc_now",
]
