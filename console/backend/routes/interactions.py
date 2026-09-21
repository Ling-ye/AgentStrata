"""Local operator controls with explicit same-origin mutation protection."""

from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from console.backend.routes.common import get_instance
from console.control import interactions

router = APIRouter(prefix="/api/bots", tags=["interactions"])


@router.get("/{instance_id}/interactions")
async def list_interactions(instance_id: str):
    try:
        return await interactions.request(get_instance(instance_id), "list")
    except Exception as exc:
        raise HTTPException(503, "交互控制暂不可用，请检查实例操作员凭据和 Gateway 状态") from exc


@router.post("/{instance_id}/interactions/{identity}/resolve")
async def resolve_interaction(instance_id: str, identity: str, request: Request):
    origin = request.headers.get("origin")
    if (
        not origin
        or urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1"}
        or urlsplit(origin).netloc != request.url.netloc
        or urlsplit(origin).scheme != request.url.scheme
        or request.headers.get("x-agentstrata-action") != "interaction"
    ):
        raise HTTPException(403, "交互决定要求同源操作")
    payload = await request.json()
    if not isinstance(payload, dict) or set(payload) != {"resolution"}:
        raise HTTPException(400, "无效交互答复")
    try:
        return await interactions.request(
            get_instance(instance_id),
            "resolve",
            identity=identity,
            resolution=payload["resolution"],
        )
    except Exception as exc:
        raise HTTPException(409, "请求已结束、答复无效或 Gateway 不可用；请刷新状态") from exc
