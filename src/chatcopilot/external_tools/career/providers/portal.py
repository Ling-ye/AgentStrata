"""Official career portals without a stable public structured endpoint."""
from __future__ import annotations

from urllib.parse import urlparse

from chatcopilot.external_tools.career.models import (
    PROVIDER_RESEARCH_FALLBACK,
    ProviderResult,
)
from chatcopilot.external_tools.career.providers.base import (
    CareerSourceProvider,
    fallback_query,
)


class SearchFallbackProvider(CareerSourceProvider):
    reason: str
    search_url: str = ""

    def search(
        self,
        *,
        keywords: tuple[str, ...],
        locations: tuple[str, ...],
        limit: int,
        posted_within_days: int,
    ) -> ProviderResult:
        del locations, limit
        return ProviderResult(
            provider=self.id,
            company=self.company,
            ok=False,
            error=self.reason,
            fallback_query=fallback_query(
                self.company,
                self.search_url or self.source_url,
                keywords,
            ),
            source_url=self.source_url,
            diagnostics={
                "fallback_required": True,
                "posted_within_days": posted_within_days,
            },
            mode=PROVIDER_RESEARCH_FALLBACK,
            snapshot_complete=False,
        )


class ByteDanceProvider(SearchFallbackProvider):
    id = "bytedance"
    company = "字节跳动"
    aliases = ("字节跳动", "ByteDance", "豆包", "Seed")
    source_url = "https://jobs.bytedance.com/experienced/position"
    official_job_hosts = ("jobs.bytedance.com",)
    reason = "字节招聘页当前未暴露可稳定依赖的公开结构化职位接口，请使用返回的 site 检索式降级查询。"

    def validate_job_url(self, source_url: str) -> None:
        super().validate_job_url(source_url)
        if urlparse(source_url).path.rstrip("/") == "/experienced/position":
            raise ValueError(
                "字节 fallback 岗位必须使用职位详情链接，不能使用职位列表页"
            )


class MiniMaxProvider(SearchFallbackProvider):
    id = "minimax"
    company = "MiniMax"
    aliases = ("MiniMax", "稀宇科技")
    source_url = "https://www.minimaxi.com/careers"
    official_job_hosts = ("minimaxi.com",)
    reason = "MiniMax 官方招聘页当前未暴露可稳定依赖的公开结构化职位接口，请使用返回的 site 检索式降级查询。"

    def validate_job_url(self, source_url: str) -> None:
        super().validate_job_url(source_url)
        if urlparse(source_url).path.rstrip("/") == "/careers":
            raise ValueError(
                "MiniMax fallback 岗位必须使用职位详情链接，不能使用招聘入口页"
            )


__all__ = ["ByteDanceProvider", "MiniMaxProvider", "SearchFallbackProvider"]
