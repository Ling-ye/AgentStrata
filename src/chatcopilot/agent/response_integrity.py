"""Deterministic, zero-model-cost checks for response integrity."""
from __future__ import annotations

import hashlib
import re
import time
from chatcopilot.contracts.execution import ResponseIntegrity as ResponseIntegrityResult


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

# Negation and conditions apply to their clause, including a coordinated list
# ("不能声称文件已修改、消息已发送"). A later independent assertion is checked
# separately; an explanation must never exempt the rest of the answer.
_CLAUSE_BOUNDARY = re.compile(r"[。！？!?；;\n]|但是|不过|然而|实际上|事实上|随后|然后|但")
_NON_ASSERTION = re.compile(
    r"(?:不能|不可|不应|不得|不要|无法)(?:直接|随意|随便|擅自)?(?:声称|宣称|说|表示|认为|确认)"
    r"|(?:没有|未曾|并未)(?:声称|宣称|表示|确认)"
    r"|(?:如果|假如|假设|倘若|只有|除非|是否|能否)"
)
_QUOTED = re.compile(r'“[^”\n]*”|「[^」\n]*」|"[^"\n]*"|`[^`\n]*`')
_QUOTE_INTRO = re.compile(r"(?:例如|比如|示例|示范|引用|原文|字符串|文档(?:中)?(?:写道|写着|提到)?)[：:、，,\s]*$")
_QUOTE_EXPLANATION = re.compile(r"^[\s，,]*(?:只是|仅是|是)(?:一个|一段)?(?:示例|例子|引用|字符串)")


def _asserted_clauses(text: str) -> list[str]:
    def mask_example(match: re.Match[str]) -> str:
        prefix = _CLAUSE_BOUNDARY.split(text[:match.start()])[-1]
        if _QUOTE_INTRO.search(prefix) or _QUOTE_EXPLANATION.search(text[match.end():]):
            return " " * len(match.group())
        return match.group()

    return _CLAUSE_BOUNDARY.split(_QUOTED.sub(mask_example, text))


def _claims_operation(text: str, pattern: re.Pattern[str]) -> bool:
    for clause in _asserted_clauses(text):
        # A comma ends a condition/negation when the next clause starts a new
        # subject; enumeration with 、 retains the original scope.
        for part in re.split(r"[，,](?=\s*(?:我|我们|文件|目录|消息|邮件|任务|作业|人格|人设|记忆))", clause):
            for match in pattern.finditer(part):
                if not _NON_ASSERTION.search(part[:match.start()]):
                    return True
    return False


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
            if _claims_operation(final_text, pattern) and not _has_receipt(
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
