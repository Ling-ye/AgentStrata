"""Read-only Tencent public career API provider."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlparse

from chatcopilot.external_tools.career.dates import is_recent, normalize_date
from chatcopilot.external_tools.career.models import (
    PROVIDER_DIRECT,
    PROVIDER_RESEARCH_FALLBACK,
    JobListing,
    ProviderResult,
)
from chatcopilot.external_tools.career.providers.base import (
    CareerSourceProvider,
    PublicHttpClient,
    fallback_query,
)


class TencentProvider(CareerSourceProvider):
    id = "tencent"
    company = "腾讯"
    aliases = ("腾讯", "Tencent", "混元")
    source_url = "https://careers.tencent.com/"
    official_job_hosts = ("careers.tencent.com",)
    endpoint = "https://careers.tencent.com/tencentcareer/api/post/Query"
    page_size = 50
    # Broad watchlist terms can match hundreds of positions. Bound each keyword
    # scan while retaining enough records for the workspace snapshot.
    max_records_per_keyword = 750

    def __init__(self, client: PublicHttpClient | None = None) -> None:
        self.client = client or PublicHttpClient()

    def search(
        self,
        *,
        keywords: tuple[str, ...],
        locations: tuple[str, ...],
        limit: int,
        posted_within_days: int,
    ) -> ProviderResult:
        jobs: dict[str, JobListing] = {}
        try:
            unique_keywords = tuple(dict.fromkeys(keywords))
            with ThreadPoolExecutor(
                max_workers=min(4, max(1, len(unique_keywords)))
            ) as executor:
                futures = [
                    executor.submit(
                        self._search_keyword,
                        keyword=keyword,
                        locations=locations,
                        posted_within_days=posted_within_days,
                    )
                    for keyword in unique_keywords
                ]
                keyword_results = [future.result() for future in futures]
            snapshot_complete = all(complete for _, complete in keyword_results)
            for keyword_jobs, _complete in keyword_results:
                jobs.update({job.identity: job for job in keyword_jobs})
        except Exception as exc:  # noqa: BLE001 - provider failures are structured
            return ProviderResult(
                provider=self.id,
                company=self.company,
                ok=False,
                error=f"腾讯官方职位接口不可用: {exc}",
                fallback_query=fallback_query(self.company, self.source_url, keywords),
                source_url=self.source_url,
                mode=PROVIDER_RESEARCH_FALLBACK,
                snapshot_complete=False,
            )
        return ProviderResult(
            provider=self.id,
            company=self.company,
            ok=True,
            jobs=tuple(jobs.values()),
            source_url=self.source_url,
            diagnostics={
                "snapshot_complete": snapshot_complete,
                "keywords_processed": len(tuple(dict.fromkeys(keywords))),
                "posted_within_days": posted_within_days,
                "stored_job_count": len(jobs),
                "return_limit": limit,
            },
            mode=PROVIDER_DIRECT,
            snapshot_complete=snapshot_complete,
        )

    def _search_keyword(
        self,
        *,
        keyword: str,
        locations: tuple[str, ...],
        posted_within_days: int,
    ) -> tuple[tuple[JobListing, ...], bool]:
        jobs: list[JobListing] = []
        page_index = 1
        fetched = 0
        complete = True
        while True:
            payload = self.client.get_json(
                self.endpoint,
                {
                    # A stable value lets PublicHttpClient's TTL cache work.
                    "timestamp": 0,
                    "attrId": 1,
                    "keyword": keyword,
                    "pageIndex": page_index,
                    "pageSize": self.page_size,
                    "language": "zh-cn",
                    "area": "cn",
                },
            )
            data = (payload or {}).get("Data") or {}
            count = int(data.get("Count") or 0)
            raw_posts = data.get("Posts") or []
            page_jobs = self.parse_payload(payload)
            fetched += len(raw_posts)
            for job in page_jobs:
                if locations and not any(value in job.location for value in locations):
                    continue
                if not job.published_on or not is_recent(
                    job.published_on,
                    posted_within_days,
                ):
                    continue
                jobs.append(job)
            if fetched >= count:
                break
            if fetched >= self.max_records_per_keyword or not raw_posts:
                complete = False
                break
            page_index += 1
        return tuple(jobs), complete

    @classmethod
    def parse_payload(cls, payload: Any) -> tuple[JobListing, ...]:
        posts = ((payload or {}).get("Data") or {}).get("Posts") or []
        out = []
        for item in posts:
            post_id = str(item.get("PostId") or item.get("RecruitPostId") or "")
            title = str(item.get("RecruitPostName") or "").strip()
            if not title:
                continue
            out.append(
                JobListing(
                    company=cls.company,
                    title=title,
                    location=str(item.get("LocationName") or "").strip(),
                    source_url=str(
                        item.get("PostURL")
                        or f"https://careers.tencent.com/jobdesc.html?postId={post_id}"
                    ).replace("http://", "https://"),
                    source_job_id=post_id,
                    responsibilities=str(item.get("Responsibility") or "").strip(),
                    requirements=str(item.get("Requirement") or "").strip(),
                    published_at=str(item.get("LastUpdateTime") or "").strip(),
                    published_on=normalize_date(item.get("LastUpdateTime")),
                    source_mode=PROVIDER_DIRECT,
                )
            )
        return tuple(out)

    def validate_job_url(self, source_url: str) -> None:
        super().validate_job_url(source_url)
        parsed = urlparse(source_url)
        if not any(key.casefold() == "postid" for key in parse_qs(parsed.query)):
            raise ValueError("腾讯 fallback 岗位必须使用包含 postId 的职位详情链接")


__all__ = ["TencentProvider"]
