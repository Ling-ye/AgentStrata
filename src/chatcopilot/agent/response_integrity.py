"""Deterministic, zero-model-cost checks for response integrity."""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass


_SUSPICIOUS_URL = re.compile(
    r"https?://(?:example\.(?:com|org|net)|(?:www\.)?fake\w*\.com|placeholder\.\w+)"
)
_CONTRADICTION = re.compile(
    r"(?:无法确认|不能确认|不知道|不清楚)[。；;\s\S]{0,160}"
    r"(?:可以确定|事实是|结论是|答案是)"
)
_VERIFICATION_CLAIM = re.compile(r"(?:已搜索|已经搜索|已查证|已经查证|已核实|已经核实)")
_SIDE_EFFECT_CLAIMS = {
    "persona": re.compile(r"(?:人格|人设).{0,16}(?:已|成功)(?:保存|设置|修改|清空)"),
    "memory": re.compile(r"(?:记忆|已记住).{0,16}(?:已|成功|保存|写入|清空)"),
    "file": re.compile(r"(?:文件|目录).{0,16}(?:已|成功)(?:保存|写入|创建|修改|删除|上传)"),
    "message": re.compile(r"(?:消息|邮件).{0,16}(?:已|成功)(?:发送|回复|转发)"),
    "task": re.compile(r"(?:任务|作业|job).{0,16}(?:已|成功)(?:创建|启动|取消|完成|提交)"),
}
_OPERATION_HINTS = {
    "persona": ("persona",),
    "memory": ("memory", "remember"),
    "file": ("file", "workspace", "document", "upload", "write", "create", "delete"),
    "message": ("message", "mail", "send", "reply", "forward"),
    "task": ("task", "job", "workflow", "delegate", "submit"),
}


@dataclass(frozen=True)
class ResponseIntegrityResult:
    ok: bool
    issues: tuple[str, ...] = ()
    evidence_digest: str = ""
    elapsed_ms: int = 0


class ResponseIntegrityCheck:
    def check(
        self,
        final_text: str,
        *,
        evidence_urls: tuple[str, ...] = (),
        successful_operations: tuple[str, ...] = (),
    ) -> ResponseIntegrityResult:
        started = time.monotonic()
        issues: list[str] = []
        for match in _SUSPICIOUS_URL.finditer(final_text):
            issues.append(f"suspicious_url:{match.group()}")
        if _CONTRADICTION.search(final_text):
            issues.append("uncertainty_followed_by_unsupported_certainty")
        normalized_operations = tuple(value.casefold() for value in successful_operations)
        if _VERIFICATION_CLAIM.search(final_text) and not (
            evidence_urls
            or _has_receipt(normalized_operations, ("search", "fetch", "browse"))
        ):
            issues.append("verification_claim_without_evidence")
        for kind, pattern in _SIDE_EFFECT_CLAIMS.items():
            if pattern.search(final_text) and not _has_receipt(
                normalized_operations,
                _OPERATION_HINTS[kind],
            ):
                issues.append(f"missing_receipt:{kind}")
        evidence_digest = hashlib.sha256(
            "\0".join(sorted(set(evidence_urls))).encode("utf-8")
        ).hexdigest()
        return ResponseIntegrityResult(
            ok=not issues,
            issues=tuple(issues),
            evidence_digest=evidence_digest,
            elapsed_ms=round((time.monotonic() - started) * 1000),
        )


def _has_receipt(operations: tuple[str, ...], hints: tuple[str, ...]) -> bool:
    return any(hint in operation for operation in operations for hint in hints)


__all__ = ["ResponseIntegrityCheck", "ResponseIntegrityResult"]
