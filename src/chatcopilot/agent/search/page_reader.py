"""Static-first URL reader with browser rendering fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from chatcopilot.contracts.tools import ToolContext, ToolDef, ToolResult

_JS_SHELL_RE = re.compile(
    r"(enable javascript|javascript is required|requires javascript|"
    r"please turn javascript on|请启用\s*javascript|需要启用\s*javascript)",
    re.IGNORECASE,
)
_BROWSER_SOLVABLE_HTTP_RE = re.compile(
    r"Error:\s*HTTP\s*(403|401|429)\b", re.IGNORECASE
)


@dataclass(frozen=True)
class PageReadResult:
    url: str
    method: str
    ok: bool
    summary: Any
    outputs: list[Any]
    actual_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "ok": self.ok,
            "summary": self.summary,
            "outputs": self.outputs,
            "actual_source": self.actual_source,
        }


class PageReader:
    """Read URLs through ``web_fetch_page`` before trying browser rendering."""

    def __init__(
        self,
        *,
        web_fetch: ToolDef | None,
        dynamic_browser: ToolDef | None,
        max_chars: int,
    ) -> None:
        self._web_fetch = web_fetch
        self._dynamic_browser = dynamic_browser
        self._max_chars = max_chars

    @property
    def available(self) -> bool:
        return self._web_fetch is not None

    def read(
        self,
        url: str,
        *,
        objective: str,
        required_fields: Sequence[str],
        allow_dynamic: bool = True,
    ) -> PageReadResult:
        if self._web_fetch is None:
            return PageReadResult(
                url=url,
                method="static",
                ok=False,
                summary="web_fetch_unavailable",
                outputs=[],
                actual_source="web_fetch",
            )
        try:
            tool_result = self._web_fetch.handler(
                {"url": url, "max_chars": self._max_chars},
                ToolContext(),
            )
        except Exception as exc:  # noqa: BLE001
            tool_result = ToolResult(ok=False, error=f"{type(exc).__name__}: {exc}")

        if not isinstance(tool_result, ToolResult):
            tool_result = ToolResult(ok=False, error="invalid_tool_result")
        summary = (
            tool_result.summary
            if tool_result.ok
            else f"Error: {tool_result.error or 'web fetch failed'}"
        )

        result = PageReadResult(
            url=url,
            method="static",
            ok=tool_result.ok,
            summary=summary,
            outputs=list(tool_result.outputs),
            actual_source="web_fetch",
        )
        if (
            allow_dynamic
            and needs_browser(summary)
            and self._dynamic_browser is not None
        ):
            return self._read_dynamic(
                url,
                objective=objective,
                required_fields=required_fields,
            )
        return result

    def read_many(
        self,
        urls: Sequence[str],
        *,
        objective: str,
        required_fields: Sequence[str],
        max_urls: int,
        allow_dynamic: bool = True,
    ) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        for url in urls[:max_urls]:
            clean_url = str(url or "").strip()
            if not clean_url:
                continue
            pages.append(
                self.read(
                    clean_url,
                    objective=objective,
                    required_fields=required_fields,
                    allow_dynamic=allow_dynamic,
                ).to_dict()
            )
        return pages

    def _read_dynamic(
        self,
        url: str,
        *,
        objective: str,
        required_fields: Sequence[str],
    ) -> PageReadResult:
        assert self._dynamic_browser is not None
        fields = [str(item) for item in required_fields if str(item).strip()]
        dynamic_args = {
            "objective": (
                f"Read the dynamically rendered page for: {objective}. "
                f"Extract these fields: {', '.join(fields)}"
            ),
            "inputs": [f"url={url}"],
            "resources": [url],
            "acceptance_criteria": fields,
            "evidence_required": ["final URL", "rendered page facts"],
        }
        try:
            tool_result = self._dynamic_browser.handler(dynamic_args, ToolContext())
        except Exception as exc:  # noqa: BLE001
            return PageReadResult(
                url=url,
                method="dynamic",
                ok=False,
                summary=f"{type(exc).__name__}: {exc}",
                outputs=[],
                actual_source="playwright",
            )
        if not isinstance(tool_result, ToolResult):
            return PageReadResult(
                url=url,
                method="dynamic",
                ok=False,
                summary="invalid_tool_result",
                outputs=[],
                actual_source="playwright",
            )
        payload = dict(tool_result.data) or _parse_payload(tool_result.summary)
        ok = tool_result.ok and payload.get("ok") is not False
        return PageReadResult(
            url=url,
            method="dynamic",
            ok=ok,
            summary=payload if payload else (tool_result.summary or tool_result.error or ""),
            outputs=list(tool_result.outputs),
            actual_source="playwright",
        )


def needs_browser(summary: Any) -> bool:
    text = str(summary or "")
    if _BROWSER_SOLVABLE_HTTP_RE.search(text):
        return True
    if text.startswith("Error:"):
        return False
    if _JS_SHELL_RE.search(text):
        return True
    content = text.split("Content:\n", 1)[-1].strip()
    return len(content) < 80


def _parse_payload(summary: Any) -> dict[str, Any]:
    try:
        value = json.loads(str(summary))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


__all__ = ["PageReader", "PageReadResult", "needs_browser"]
