"""MCP response serialization and compaction helpers."""
from __future__ import annotations

import json
import re
from typing import Any

def _serialize_call_result(result: Any, *, max_chars: int) -> str:
    payload: dict[str, Any] = {
        "is_error": bool(getattr(result, "isError", False)),
    }
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        payload["structured"] = structured
    content_items: list[dict[str, Any]] = []
    for item in getattr(result, "content", []) or []:
        item_type = getattr(item, "type", "")
        if item_type == "text":
            content_items.append({"type": "text", "text": getattr(item, "text", "")})
        else:
            content_items.append({"type": item_type or type(item).__name__})
    if content_items:
        payload["content"] = content_items
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > max_chars:
        text = _compact_mcp_response(payload, max_chars)
    return text


def _compact_mcp_response(payload: dict[str, Any], max_chars: int) -> str:
    """Structurally compact verbose MCP responses before falling back to truncation.

    Strips image URLs, tracking URLs, verbose promotion metadata, avatars,
    cover data, and limits result lists to save tokens.  Works across
    xiaohongshu, taoke (Taobao/PDD), and similar MCP payloads.
    """
    content_items = payload.get("content")
    if not isinstance(content_items, list):
        return json.dumps(payload, ensure_ascii=False)[:max_chars] + "\n...[truncated]"

    for item in content_items:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        raw_text = item.get("text", "")
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        image_candidates = _extract_image_candidates(data)
        _strip_verbose_fields(data)
        if image_candidates:
            data["image_candidates"] = image_candidates
        item["text"] = json.dumps(data, ensure_ascii=False)

    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[truncated]"
    return text


_STRIP_KEYS = frozenset({
    # -- xiaohongshu --
    "avatar", "urlPre", "urlDefault", "fileId",
    "infoList", "imageList", "cover", "livePhoto",
    # -- taoke / taobao / pdd --
    "click_url", "coupon_share_url",           # long Base64 tracking URLs
    "publish_info",                             # affiliate commission block
    "scope_info", "presale_info",               # usually empty / irrelevant
    "more_promotion_list",                      # duplicate verbose promotions
    "final_promotion_path_list",                # full promotion path detail
    "promotion_tag_list",                       # redundant with final_promotion_price
    "activity_tags",                            # opaque numeric tag arrays
    "small_images", "pict_url", "white_image",  # product image URLs
    "goods_image_url", "goods_thumbnail_url",   # pdd image URLs
    "search_id",                                # internal pdd search id
    "goods_sign",                               # opaque goods signature
    "subsidy_list", "platform_discount_list",   # usually empty arrays
})

_IMAGE_URL_KEYS = frozenset({
    "url", "pict_url", "white_image", "goods_image_url",
    "goods_thumbnail_url", "cover",
})

_IMAGE_CANDIDATE_KEYS = frozenset({
    "url", "image", "images", "image_url", "image_urls", "imageList",
    "pict_url", "white_image", "small_images", "goods_image_url",
    "goods_thumbnail_url", "cover", "thumbnail", "thumb", "pic", "picture",
})
_IMAGE_CANDIDATE_SKIP_KEYS = frozenset({"avatar", "icon", "logo"})
_IMAGE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp)(?:[?#].*)?$", re.IGNORECASE)


def _extract_image_candidates(obj: Any, *, limit: int = 5) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def _walk(value: Any, context: dict[str, Any], path: tuple[str, ...]) -> None:
        if len(candidates) >= limit:
            return
        if isinstance(value, dict):
            next_context = value
            for key, child in value.items():
                _walk(child, next_context, (*path, str(key)))
            return
        if isinstance(value, list):
            for item in value:
                _walk(item, context, path)
            return
        if not isinstance(value, str):
            return
        url = value.strip()
        if not _looks_like_image_url(url):
            return
        key = path[-1] if path else ""
        if key in _IMAGE_CANDIDATE_SKIP_KEYS or any(p in _IMAGE_CANDIDATE_SKIP_KEYS for p in path):
            return
        if not _path_suggests_image(path) and not _IMAGE_EXT_RE.search(url):
            return
        if url in seen:
            return
        seen.add(url)
        item = {
            "image_url": url,
            "source": _short_text(_context_value(context, ("source", "platform", "type", "modelType"))),
            "title": _short_text(_context_value(context, ("title", "display_title", "desc", "name", "goods_name", "item_title"))),
            "source_url": _short_text(_context_source_url(context, image_url=url)),
        }
        candidates.append({k: v for k, v in item.items() if v})

    _walk(obj, {}, ())
    return candidates


def _looks_like_image_url(value: str) -> bool:
    if not _IMAGE_URL_RE.match(value):
        return False
    lowered = value.lower()
    if "base64," in lowered:
        return False
    return (
        _IMAGE_EXT_RE.search(value) is not None
        or any(token in lowered for token in ("image", "img", "pic", "pict", "photo", "cover", "thumbnail"))
    )


def _path_suggests_image(path: tuple[str, ...]) -> bool:
    for part in path:
        key = part.strip()
        if key in _IMAGE_CANDIDATE_KEYS:
            return True
        lowered = key.lower()
        if any(token in lowered for token in ("image", "img", "pic", "pict", "photo", "cover", "thumb")):
            return True
    return False


def _context_value(context: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = context.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _context_source_url(context: dict[str, Any], *, image_url: str) -> str:
    for key in ("source_url", "sourceUrl", "link", "share_url", "shareUrl", "url"):
        value = context.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text and text != image_url and _IMAGE_URL_RE.match(text):
                return text
    return ""


def _short_text(value: str, *, max_len: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _strip_verbose_fields(obj: Any, *, depth: int = 0) -> None:
    """Recursively remove known verbose keys and limit list lengths."""
    if depth > 12:
        return
    if isinstance(obj, dict):
        for key in list(obj.keys()):
            if key in _STRIP_KEYS:
                obj.pop(key)
            elif key in _IMAGE_URL_KEYS and isinstance(obj[key], str):
                obj.pop(key)
            else:
                _strip_verbose_fields(obj[key], depth=depth + 1)
        # -- xiaohongshu limits --
        sub_comments = obj.get("subComments")
        if isinstance(sub_comments, list) and len(sub_comments) > 3:
            obj["subComments"] = sub_comments[:3]
        comments = obj.get("comments")
        if isinstance(comments, dict):
            comment_list = comments.get("list")
            if isinstance(comment_list, list) and len(comment_list) > 5:
                comments["list"] = comment_list[:5]
        feeds = obj.get("feeds")
        if isinstance(feeds, list):
            feeds = [
                f for f in feeds
                if not (isinstance(f, dict) and f.get("modelType") == "hot_query")
            ]
            if len(feeds) > 8:
                feeds = feeds[:8]
            obj["feeds"] = feeds
        # -- taoke / taobao result list limits --
        map_data = obj.get("map_data")
        if isinstance(map_data, list) and len(map_data) > 5:
            obj["map_data"] = map_data[:5]
        # -- pdd goods list limits --
        goods_list = obj.get("goods_list")
        if isinstance(goods_list, list) and len(goods_list) > 5:
            obj["goods_list"] = goods_list[:5]
    elif isinstance(obj, list):
        for item in obj:
            _strip_verbose_fields(item, depth=depth + 1)


__all__ = ["_compact_mcp_response", "_serialize_call_result"]
