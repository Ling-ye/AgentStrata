"""Career intelligence orchestration independent of Agent and platform layers."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from chatcopilot.external_tools.career.models import (
    PROVIDER_RESEARCH_FALLBACK,
    ProviderResult,
)
from chatcopilot.external_tools.career.providers import get_providers
from chatcopilot.external_tools.career.store import CareerIntelStore, utc_now


class CareerIntelService:
    def __init__(self, workspace_root: Path) -> None:
        self.store = CareerIntelStore(workspace_root)

    def search_jobs(
        self,
        *,
        companies: list[str] | None = None,
        keywords: list[str] | None = None,
        locations: list[str] | None = None,
        posted_within_days: int = 30,
        limit_per_company: int = 20,
    ) -> dict[str, Any]:
        if limit_per_company < 1 or limit_per_company > 50:
            raise ValueError("limit_per_company 必须在 1 到 50 之间")
        if posted_within_days < 1 or posted_within_days > 3650:
            raise ValueError("posted_within_days 必须在 1 到 3650 之间")
        watchlist = self.store.get_watchlist()
        selected_companies = companies or watchlist["companies"]
        selected_keywords = keywords or watchlist["keywords"]
        selected_locations = locations if locations is not None else watchlist["locations"]
        if not selected_companies:
            raise ValueError("请显式指定至少一个公司，或先更新当前用户的关注公司")
        providers = [
            provider
            for provider in get_providers()
            if any(
                name.casefold() in {alias.casefold() for alias in provider.aliases}
                or name.casefold() == provider.company.casefold()
                for name in selected_companies
            )
        ]
        unknown = [
            name for name in selected_companies
            if not any(name.casefold() in {a.casefold() for a in p.aliases} for p in providers)
        ]
        started_at = utc_now()
        results = []
        if providers:
            with ThreadPoolExecutor(max_workers=min(3, len(providers))) as executor:
                futures = [
                    executor.submit(
                        provider.search,
                        keywords=tuple(selected_keywords),
                        locations=tuple(selected_locations),
                        limit=limit_per_company,
                        posted_within_days=posted_within_days,
                    )
                    for provider in providers
                ]
                results.extend(future.result() for future in futures)
        terms = " OR ".join(selected_keywords)
        results.extend(
            ProviderResult(
                provider="search_information",
                company=company,
                ok=False,
                error="该公司未配置专用 provider，请使用联网研究并将官方岗位链接写回。",
                fallback_query=f'"{company}" ({terms}) 招聘',
                mode=PROVIDER_RESEARCH_FALLBACK,
                snapshot_complete=False,
                diagnostics={"fallback_required": True, "posted_within_days": posted_within_days},
            )
            for company in unknown
        )
        report = self.store.record_scan(
            started_at=started_at,
            companies=selected_companies,
            keywords=selected_keywords,
            locations=selected_locations,
            posted_within_days=posted_within_days,
            results=results,
        )
        for key in ("new_jobs", "changed_jobs"):
            report[key] = _limit_by_company(report[key], limit_per_company)
        report["unknown_companies"] = unknown
        report["filters"] = {
            "companies": selected_companies,
            "keywords": selected_keywords,
            "locations": selected_locations,
            "posted_within_days": posted_within_days,
        }
        report["note"] = (
            "只有相同 scope 的连续两次完整 direct 扫描均未发现，才标记 suspected_closed；"
            "research_fallback 只记录未再次发现。"
        )
        return report

    def ingest_jobs(
        self,
        *,
        records: list[dict[str, Any]],
        scan_scope: dict[str, Any],
    ) -> dict[str, Any]:
        return self.store.ingest_jobs(records, scan_scope=scan_scope)


def _limit_by_company(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for item in sorted(
        records,
        key=lambda value: (
            -int(value.get("relevance_score") or 0),
            value.get("company") or "",
            value.get("title") or "",
        ),
    ):
        company = str(item.get("company") or "")
        if counts.get(company, 0) >= limit:
            continue
        counts[company] = counts.get(company, 0) + 1
        selected.append(item)
    return selected


__all__ = ["CareerIntelService"]
