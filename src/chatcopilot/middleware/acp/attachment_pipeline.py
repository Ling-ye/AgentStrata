"""User file storage capability for ACP/cc-connect uploads.

本模块负责"file upload only"的确定性短路链：从 ACP prompt block 提取
文本与资源引用、把 cc-connect transport 文件搬到当前会话空间、生成
保存确认 / 占位文案。

"一个 block 是不是真实文件附件"的判定**只在**
:mod:`chatcopilot.middleware.acp.attachment_classifier` 里维护，本模块
不再持有任何 ``_RESOURCE_SOURCE_KEYS`` / ``_pick_resource_name_*``
风格的并行实现。详见 :func:`attachment_classifier.classify_resource_block`。
"""
from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chatcopilot.contracts.workspace import WORKSPACE_SCOPE_GROUP_SHARED
from chatcopilot.middleware.acp.attachment_classifier import (
    ClassifiedResource,
    ResourceKind,
    classify_resource_block,
    is_plausible_file_basename,
    resource_basename,
)
from chatcopilot.core.workspace_runtime import Workspace, resolve_workspace_root

_LOGGER = logging.getLogger("chatcopilot.middleware.acp.attachment_pipeline")


@dataclass(frozen=True)
class ExtractedPrompt:
    """Structured prompt parts extracted from ACP prompt blocks."""

    text: str
    resource_names: list[str]
    resource_count: int = 0

    @property
    def has_resource(self) -> bool:
        return self.resource_count > 0 or bool(self.resource_names)


# ---------------------------------------------------------------------------
# 文本侧正则（与 classifier 无关，纯文本兜底）
# ---------------------------------------------------------------------------

_TASK_VERB_EN_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:diff|csvtools?|trend|summary|summarize|analy[sz]e|analysis|correlation|preview|top)"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_TASK_VERB_ZH_RE = re.compile(r"对比|比较|分析|趋势|汇总|归因|解读|预览|处理|生成报告|同步|迁移|转移|入库|写入|导入|更新")
_NEGATED_TASK_RE = re.compile(r"不(?:要|用|必)?(?:分析|对比|比较|处理|解读)|先别(?:分析|对比|处理)")
_ATTACHMENT_TEXT_HINT_RE = re.compile(
    r"附件|文件|上传|uploaded|attachment|resource|file|\.cc-connect|attachments",
    re.IGNORECASE,
)
_ATTACHMENT_FILE_RE = re.compile(
    r"(?i)(?:MemoryReport|memory_report|mono|monotree|moduleAlloc|vma|\.csv|\.json|\.zip|\.tar\.gz|\.tgz|\.tar)"
)
_ATTACHMENT_TEXT_FILE_RE = re.compile(
    r"(?i)(?:\[文件\]|\bfile[:：]|\battachment[:：])\s*([^\r\n]+)"
)
_ATTACHMENT_TOKEN_RE = re.compile(
    r"(?i)([^\s\[\]，,；;]+(?:\.moduleAlloc|\.csv|\.json|\.zip|\.tar\.gz|\.tgz|\.tar|\.memory_report|\.monotree))"
)
_ATTACHMENT_PATH_TOKEN_RE = re.compile(
    r"(?i)(@?(?:file://|[A-Za-z]:[\\/]|[/\\])"
    r"[^\s\[\]，,；;]+?\.[A-Za-z0-9][A-Za-z0-9._-]*)"
)
_WEB_URL_RE = re.compile(
    r"(?i)https?://[^\s\[\]，,；;]+"
)
_TEXTIFIED_ATTACHMENT_LINE_RE = re.compile(
    r"(?i)^\s*(?:\[文件\]|\bfile[:：]|\battachment[:：])\s*(?P<name>.+?)\s*$"
)
_FEISHU_FILE_SIZE_LIMIT_CODE_RE = re.compile(r"\bcode\s*[=:]\s*234037\b", re.IGNORECASE)
_FEISHU_FILE_SIZE_LIMIT_TEXT_RE = re.compile(
    r"downloaded\s+file\s+size\s+exceeds\s+limit|file\s+size\s+exceeds\s+limit",
    re.IGNORECASE,
)
# cc-connect 在调用 ACP 前把已落盘文件和图片追加为固定尾缀。该尾缀不是
# transport hook 见证的用户正文，必须先还原为资源引用再做正文摘要校验。
_CC_CONNECT_RESOURCE_SUFFIX_RE = re.compile(
    r"\n*\(\s*(?P<kind>"
    r"Files?\s+saved\s+locally\s*,\s*please\s+read\s+them"
    r"|Image\s+files?\s+saved\s+locally"
    r")\s*:\s*(?P<paths>[^)]+)\)\s*$",
    re.IGNORECASE,
)
_CC_CONNECT_FILE_DEFAULT = "Please analyze the attached file(s)."
_CC_CONNECT_IMAGE_DEFAULT = "User sent image(s)."


# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------

def dedupe_resource_names(names: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    return deduped


# 这些容器键用于在 _walk_for_resources 里下钻：ACP 可能把真正的 file block
# 套在 ``content`` / ``attachments`` / ``file`` 等容器里推过来。
_RESOURCE_RECURSION_KEYS: tuple[str, ...] = (
    "content",
    "contents",
    "resource",
    "resources",
    "annotation",
    "annotations",
    "attachment",
    "attachments",
    "file",
    "files",
)


def _walk_for_resources(value: Any) -> tuple[int, list[str]]:
    """递归遍历 prompt block 树，仅收集分类为 FILE 的资源名。

    判定的真理只在 :func:`classify_resource_block`：
    - dict / object 节点 → 跑一次 classify
        - FILE：返回 1 + name，**不再下钻**（避免父子重复计数）
        - WEB_URL：返回 0，**不下钻**（链接 block 的子字段是 URL 参数）
        - UNKNOWN：返回 0，但**下钻**到子字段继续找
    - list / tuple → 逐元素递归
    - scalar → 0
    """
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return 0, []

    if isinstance(value, (list, tuple)):
        total = 0
        names: list[str] = []
        for item in value:
            count, item_names = _walk_for_resources(item)
            total += count
            names.extend(item_names)
        return total, names

    classified: ClassifiedResource = classify_resource_block(value)
    if classified.is_file:
        return 1, [classified.name]
    if classified.kind is ResourceKind.WEB_URL:
        return 0, []

    total = 0
    names: list[str] = []

    if isinstance(value, dict):
        # 容器键优先，但若上游用了别的命名，也尝试遍历所有字段值。
        for key in _RESOURCE_RECURSION_KEYS:
            if key in value:
                count, item_names = _walk_for_resources(value[key])
                total += count
                names.extend(item_names)
        for key, raw in value.items():
            if key in _RESOURCE_RECURSION_KEYS:
                continue
            if isinstance(raw, (list, tuple, dict)):
                count, item_names = _walk_for_resources(raw)
                total += count
                names.extend(item_names)
        return total, names

    # pydantic model / generic object
    for key in _RESOURCE_RECURSION_KEYS:
        child = getattr(value, key, None)
        if child is None:
            continue
        count, item_names = _walk_for_resources(child)
        total += count
        names.extend(item_names)
    return total, names


# ---------------------------------------------------------------------------
# Prompt 解析
# ---------------------------------------------------------------------------

def extract_prompt_parts(prompt_blocks: list) -> ExtractedPrompt:
    """Extract text and resource references from ACP prompt blocks."""
    text_parts: list[str] = []
    resource_names: list[str] = []
    resource_count = 0
    for block in prompt_blocks or []:
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
        else:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_parts.append(text)

        count, names = _walk_for_resources(block)
        resource_count += count
        resource_names.extend(names)
    return ExtractedPrompt(
        text="\n".join(s for s in text_parts if s).strip(),
        resource_names=dedupe_resource_names(resource_names),
        resource_count=resource_count,
    )


def normalize_cc_connect_wrapper(prompt_parts: ExtractedPrompt) -> ExtractedPrompt:
    """把 cc-connect 追加的文件/图片尾缀还原成结构化资源引用。

    只剥离完整匹配的末尾协议段；用户正文和已有 ACP resource block 均保留。
    抽不出合法文件名时保持原样，让身份摘要校验继续失败关闭。
    """
    text = prompt_parts.text or ""
    if not text:
        return prompt_parts

    wrapper_names: list[str] = []
    saw_file_suffix = False
    saw_image_suffix = False
    stripped = text
    while match := _CC_CONNECT_RESOURCE_SUFFIX_RE.search(stripped):
        kind = (match.group("kind") or "").lower()
        is_image = kind.startswith("image")
        if (is_image and saw_image_suffix) or (not is_image and saw_file_suffix):
            break
        raw_paths = match.group("paths") or ""
        candidates = re.split(r"[,\n|]+", raw_paths)
        basenames = [resource_basename(path) for path in candidates if path]
        names = dedupe_resource_names(
            [name for name in basenames if is_plausible_file_basename(name)]
        )
        if not names:
            break
        wrapper_names = names + wrapper_names
        saw_image_suffix = saw_image_suffix or is_image
        saw_file_suffix = saw_file_suffix or not is_image
        stripped = stripped[: match.start()].rstrip()

    if not wrapper_names:
        return prompt_parts

    for saw_suffix, default_text in (
        (saw_file_suffix, _CC_CONNECT_FILE_DEFAULT),
        (saw_image_suffix, _CC_CONNECT_IMAGE_DEFAULT),
    ):
        if not saw_suffix:
            continue
        trimmed = stripped.rstrip()
        if trimmed == default_text:
            stripped = ""
        elif trimmed.endswith("\n" + default_text):
            stripped = trimmed[: -len(default_text) - 1].rstrip()

    existing_names = dedupe_resource_names(prompt_parts.resource_names)
    resource_names = dedupe_resource_names(existing_names + wrapper_names)
    added_count = len([name for name in resource_names if name not in existing_names])
    return ExtractedPrompt(
        text=stripped.strip(),
        resource_names=resource_names,
        resource_count=prompt_parts.resource_count + added_count,
    )


# ---------------------------------------------------------------------------
# 飞书附件超限错误
# ---------------------------------------------------------------------------

def is_feishu_file_size_limit_error(text: str) -> bool:
    """Return whether cc-connect/lark reports a Feishu attachment size-limit failure."""
    normalized = (text or "").strip()
    if not normalized:
        return False

    if _FEISHU_FILE_SIZE_LIMIT_CODE_RE.search(normalized):
        return True

    lowered = normalized.lower()
    has_download_failure = (
        "download file failed" in lowered
        or "resource api" in lowered
        or "feishu:" in lowered
        or "lark" in lowered
    )
    return has_download_failure and bool(_FEISHU_FILE_SIZE_LIMIT_TEXT_RE.search(normalized))


def format_feishu_file_size_limit_reply() -> str:
    """Build the user-facing reply for Feishu attachment download size-limit failures."""
    return (
        "文件太大，飞书机器人无法下载这个附件。\n"
        "请压缩或拆分文件后重新发送，或改用可访问的链接/共享目录再告诉我。"
    )


# ---------------------------------------------------------------------------
# 文本侧"任务动词" / "附件提及"识别
# ---------------------------------------------------------------------------

def has_task_verb(text: str) -> bool:
    """Return whether plain text explicitly authorizes a processing task."""
    normalized = (text or "").strip()
    if not normalized:
        return False
    if _NEGATED_TASK_RE.search(normalized):
        return False
    return bool(_TASK_VERB_EN_RE.search(normalized) or _TASK_VERB_ZH_RE.search(normalized))


def _without_web_urls(text: str) -> str:
    """Remove HTTP(S) URLs before applying local attachment fallback regexes."""
    return _WEB_URL_RE.sub("", text or "")


def has_text_attachment_reference(text: str) -> bool:
    """Return whether plain text contains a textified attachment reference."""
    normalized = _without_web_urls(text).strip()
    if not normalized:
        return False

    file_markers = _ATTACHMENT_FILE_RE.findall(normalized)
    if _ATTACHMENT_PATH_TOKEN_RE.search(normalized):
        return True
    if _ATTACHMENT_TOKEN_RE.search(normalized):
        return True
    if _ATTACHMENT_TEXT_HINT_RE.search(normalized) and file_markers:
        return True
    if len(file_markers) >= 2:
        return True
    return False


def looks_like_attachment_upload_text(text: str) -> bool:
    """Backward-compatible wrapper for textified attachment detection."""
    return has_text_attachment_reference(text)


def extract_attachment_names_from_text(text: str) -> list[str]:
    """Extract filenames from cc-connect textified attachment prompts."""
    normalized = _without_web_urls(text)
    candidates: list[str] = []
    for match in _ATTACHMENT_TEXT_FILE_RE.finditer(normalized):
        raw = match.group(1).strip()
        if raw:
            candidates.append(raw)
    for match in _ATTACHMENT_TOKEN_RE.finditer(normalized):
        raw = match.group(1).strip()
        if raw:
            candidates.append(raw)
    for match in _ATTACHMENT_PATH_TOKEN_RE.finditer(normalized):
        raw = match.group(1).strip()
        if raw:
            candidates.append(raw.removeprefix("@"))
    basenames = [resource_basename(item).strip() for item in candidates if item.strip()]
    return dedupe_resource_names(
        [name for name in basenames if is_plausible_file_basename(name)]
    )


def is_textified_attachment_upload_only(text: str) -> bool:
    """Return whether text is a cc-connect file notification without a user task."""
    lines = [
        line.strip()
        for line in _without_web_urls(text).splitlines()
        if line.strip()
    ]
    if not lines:
        return False

    seen_file_line = False
    for index, line in enumerate(lines):
        if index == 0 and re.match(r"^(?:回复\s+)?[^:：]{1,80}[:：]\s*$", line):
            continue
        match = _TEXTIFIED_ATTACHMENT_LINE_RE.match(line)
        if match and resource_basename(match.group("name")):
            seen_file_line = True
            continue
        if _ATTACHMENT_TOKEN_RE.fullmatch(line) or _ATTACHMENT_PATH_TOKEN_RE.fullmatch(line):
            seen_file_line = True
            continue
        return False
    return seen_file_line


# ---------------------------------------------------------------------------
# 附件搬运
# ---------------------------------------------------------------------------

def transport_attachment_inbox(ws: Workspace) -> Path:
    """cc-connect static work_dir attachment inbox."""
    return resolve_workspace_root(ws) / "default" / ".cc-connect" / "attachments"


def import_transport_attachments(ws: Workspace, resource_names: list[str]) -> list[str]:
    """Import current-turn cc-connect attachments into the user private workspace."""
    # cc-connect v1.4 writes every project attachment to one static work_dir
    # inbox using only its basename. That location carries neither the chat nor
    # the message identity, so concurrent QQ groups can overwrite or claim one
    # another's files. Existing files already present in the exact shared
    # workspace remain usable; the ambiguous legacy inbox is fail-closed.
    if getattr(ws, "scope", "actor") == WORKSPACE_SCOPE_GROUP_SHARED:
        return []
    inbox = transport_attachment_inbox(ws)
    try:
        if inbox.resolve() == ws.attachments.resolve():
            return []
    except OSError:
        return []
    if not inbox.is_dir() or inbox.is_symlink():
        return []

    requested = [resource_basename(name) for name in resource_names if name]
    requested = dedupe_resource_names([name for name in requested if name])
    if not requested:
        return []

    imported: list[str] = []
    missing: list[str] = []
    ws.attachments.mkdir(parents=True, exist_ok=True)
    for name in requested:
        if "/" in name or "\\" in name or name in {".", ".."}:
            continue
        src_candidate = inbox / name
        if src_candidate.is_symlink():
            missing.append(name)
            continue
        src = src_candidate.resolve()
        dst = (ws.attachments / name).resolve()
        try:
            inbox_root = inbox.resolve()
            if (
                not src.is_file()
                or not src.is_relative_to(inbox_root)
                or not ws.is_inside(dst)
            ):
                missing.append(name)
                continue
            if src != dst:
                shutil.copy2(src, dst)
                try:
                    src.unlink()
                except OSError:
                    _LOGGER.warning("transport attachment copied but not removed | src=%s", src)
            imported.append(name)
        except OSError:
            _LOGGER.exception("transport attachment import failed | src=%s dst=%s", src, dst)
    if missing:
        _LOGGER.info(
            "transport attachment not ready | inbox=%s workspace=%s missing=%s",
            inbox,
            ws.root,
            ",".join(missing),
        )
    return dedupe_resource_names(imported)


def confirmed_transport_attachments(
    ws: Workspace,
    resource_names: list[str],
    *,
    imported_names: list[str] | None = None,
) -> list[str]:
    """Return files proven to belong to this transport turn.

    A shared QQ group may already contain a same-named file from an earlier
    actor.  A basename-only ACP resource is therefore not evidence that the
    existing inode belongs to the current upload.  Until the transport exposes
    a chat/message-bound path or token, group turns accept only names returned
    by the current import operation (which the legacy static inbox deliberately
    cannot provide).
    """

    requested = dedupe_resource_names(
        [resource_basename(name) for name in resource_names if name]
    )
    if getattr(ws, "scope", "actor") == WORKSPACE_SCOPE_GROUP_SHARED:
        imported = set(
            dedupe_resource_names(
                [resource_basename(name) for name in (imported_names or []) if name]
            )
        )
        requested = [name for name in requested if name in imported]

    confirmed: list[str] = []
    for name in requested:
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            continue
        candidate = ws.attachments / name
        try:
            if candidate.is_symlink():
                continue
            target = candidate.resolve(strict=True)
            if target.is_file() and ws.is_inside(target):
                confirmed.append(name)
        except OSError:
            continue
    return dedupe_resource_names(confirmed)


def format_group_attachment_binding_rejection(resource_names: list[str]) -> str:
    names = "、".join(dedupe_resource_names([name for name in resource_names if name]))
    subject = f"（{names}）" if names else ""
    return (
        f"已拒绝接收这次群附件{subject}：当前 QQ 接入只提供文件名，"
        "不能把文件安全绑定到本群、本条消息和当前发送者，也不能据此复用群里同名旧文件。\n"
        "如果文件已经在当前群共享空间，请改用文字让我列出共享文件后再指定处理；"
        "新的群附件需等待接入层提供 message-bound 路径或令牌。"
    )


# ---------------------------------------------------------------------------
# 短路 / 收口
# ---------------------------------------------------------------------------

def should_short_circuit_attachment_only(prompt_parts: ExtractedPrompt) -> bool:
    """Return whether this turn is only a file transfer acknowledgement."""
    if is_textified_attachment_upload_only(prompt_parts.text):
        return True
    if has_task_verb(prompt_parts.text):
        return False
    return prompt_parts.has_resource or has_text_attachment_reference(prompt_parts.text)


def collect_attachment_references(
    prompt_parts: ExtractedPrompt,
    text: str,
) -> list[str]:
    """统一返回当前 prompt 中所有候选附件文件名。

    优先使用 ACP resource block 解析结果（已由 classifier 在
    :func:`extract_prompt_parts` 阶段过滤为 FILE）。若 ACP 端没有结构化
    资源（cc-connect 把附件转成纯文本 ``[文件]`` 标记的常规场景），
    再退化到 :func:`extract_attachment_names_from_text`。
    """
    if prompt_parts.has_resource and prompt_parts.resource_names:
        # classifier 已经过滤过，basename 已规整，直接 dedupe 返回即可。
        return dedupe_resource_names(
            [name for name in prompt_parts.resource_names if name]
        )
    return extract_attachment_names_from_text(text or "")


# ---------------------------------------------------------------------------
# 文案生成（同步发给 cc-connect 的 receipt / ack）
# ---------------------------------------------------------------------------

def format_attachment_receipt(
    resource_names: list[str],
    workspace: Workspace | None = None,
) -> str:
    """生成纯文件上传命中短路后立即同步返回给 cc-connect 的占位文案。

    在 ``_schedule_attachment_ack`` 异步等文件落盘的这段时间里，ACP server
    必须先给 cc-connect 一条 ``session_update``，否则 UI 会显示
    ``(empty response)``。占位的目标只是告诉用户消息已被接住，不需要列出
    最终路径——3 秒后的 debounced ack 会补一条完整的"文件已保存"。
    """
    safe = _sanitize_display_names(resource_names)
    if not safe:
        return "已收到文件消息，正在确认保存状态…"
    joined = "、".join(safe)
    return (
        f"已收到附件：{joined}。\n"
        f"正在保存到{_workspace_space_label(workspace)}，稍候我会再发一条确认。"
    )


def format_attachment_deferred_receipt(
    resource_names: list[str],
    workspace: Workspace | None = None,
) -> str:
    """LLM 兜底路径上"文件还没落盘"时的占位文案（与短路 6 的 receipt 平行）。

    与 :func:`format_attachment_receipt` 区别只在最后一句：deferred 路径
    需要提示用户重新发起 diff / 对比请求；receipt 路径会有 debounced ack
    自动续上。两条路径共用一个 ``names`` 清洗 + dedupe + basename 规整逻辑
    （:func:`_sanitize_display_names`），未来文案漂移只改这一文件。
    """
    safe = _sanitize_display_names(resource_names)
    space = _workspace_space_label(workspace)
    if not safe:
        return (
            f"已收到附件，正在保存到{space}。\n"
            "保存完成后我会再发一条确认，再请你重新发起 diff / 对比请求。"
        )
    joined = "、".join(safe)
    return (
        f"已收到附件：{joined}，正在保存到{space}。\n"
        "保存完成后我会再发一条确认，再请你重新发起 diff / 对比请求。"
    )


def _sanitize_display_names(resource_names: list[str]) -> list[str]:
    """把"要展示给用户的附件名"做最终清洗：basename + dedupe + 非空 + 白名单。

    即便上游 classifier 出 bug 误把 WEB_URL 名字混进来（双保险），这里也
    会被 :func:`is_plausible_file_basename` 拦截，避免"已收到附件：
    example.feishu.cn" 这种文案再次出现。
    """
    basenames = [resource_basename(name) for name in resource_names if name]
    deduped = dedupe_resource_names([name for name in basenames if name])
    return [name for name in deduped if is_plausible_file_basename(name)]


def format_attachment_ack(ws: Workspace, resource_names: list[str]) -> str:
    """Build the upload-only acknowledgement with workspace-relative attachment paths."""
    entries: list[tuple[float, str, str]] = []
    try:
        if ws.attachments.is_dir():
            for path in ws.attachments.iterdir():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if path.is_file():
                    entries.append((stat.st_mtime, path.name, f"attachments/{path.name}"))
        entries.sort(key=lambda item: item[0], reverse=True)
    except OSError:
        entries = []

    all_paths = [display_path for _mtime, _name, display_path in entries]
    if not all_paths:
        all_paths = [f"attachments/{name}" for name in resource_names if name]
    seen: set[str] = set()
    deduped_paths: list[str] = []
    for display_path in all_paths:
        if display_path in seen:
            continue
        seen.add(display_path)
        deduped_paths.append(display_path)

    current_paths = [f"attachments/{name}" for name in resource_names if name] or deduped_paths[:2]
    current = "、".join(current_paths)
    visible_paths = deduped_paths[:50]
    space = _workspace_space_label(ws)
    lines = [f"文件已保存到{space}：{current}。" if current else f"文件已保存到{space}。"]
    if visible_paths:
        subject = (
            "当前群"
            if getattr(ws, "scope", "actor") == WORKSPACE_SCOPE_GROUP_SHARED
            else "你当前"
        )
        lines.append(f"{subject}累计 {len(deduped_paths)} 个已保存文件：")
        lines.extend(f"- {display_path}" for display_path in visible_paths)
        if len(deduped_paths) > len(visible_paths):
            lines.append(f"- 还有 {len(deduped_paths) - len(visible_paths)} 个文件未展示")
    lines.extend(["", "请告诉我下一步要做什么。"])
    return "\n".join(lines)


def _workspace_space_label(workspace: Workspace | None) -> str:
    if (
        workspace is not None
        and getattr(workspace, "scope", "actor") == WORKSPACE_SCOPE_GROUP_SHARED
    ):
        return "当前群共享空间"
    return "你的私人空间"


__all__ = [
    "ClassifiedResource",
    "ExtractedPrompt",
    "ResourceKind",
    "classify_resource_block",
    "collect_attachment_references",
    "dedupe_resource_names",
    "extract_attachment_names_from_text",
    "extract_prompt_parts",
    "format_attachment_ack",
    "format_attachment_deferred_receipt",
    "format_attachment_receipt",
    "format_feishu_file_size_limit_reply",
    "format_group_attachment_binding_rejection",
    "confirmed_transport_attachments",
    "has_task_verb",
    "has_text_attachment_reference",
    "import_transport_attachments",
    "is_feishu_file_size_limit_error",
    "is_plausible_file_basename",
    "is_textified_attachment_upload_only",
    "looks_like_attachment_upload_text",
    "normalize_cc_connect_wrapper",
    "resource_basename",
    "should_short_circuit_attachment_only",
    "transport_attachment_inbox",
]
