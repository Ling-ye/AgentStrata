from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from console.backend.routes.common import get_instance, start_task
from console.backend.sse import sse
from console.control import catalog, inventory, observability, operations
from console.control.discovery import discover_instances, repo_root

router = APIRouter(prefix="/api/bots", tags=["bots"])


@router.get("")
def list_bots():
    return [i.to_dict() for i in discover_instances()]


@router.get("/{instance_id}/status")
def bot_status(instance_id: str):
    return operations.status(get_instance(instance_id))


@router.get("/{instance_id}/inventory")
def bot_inventory(instance_id: str):
    return inventory.bot_inventory(get_instance(instance_id))


@router.get("/{instance_id}/jobs")
def bot_jobs(instance_id: str, limit: int = 50):
    return operations.jobs(get_instance(instance_id), limit=limit)


@router.get("/{instance_id}/tasks")
def bot_tasks(instance_id: str, limit: int = 50):
    return operations.tasks(get_instance(instance_id), limit=limit)


@router.get("/{instance_id}/tasks/{task_id}")
def bot_task_detail(instance_id: str, task_id: str):
    try:
        result = operations.task_detail(get_instance(instance_id), task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    return result


@router.get("/{instance_id}/tasks/{task_id}/events")
def bot_task_events(
    instance_id: str,
    task_id: str,
    response: Response,
    limit: int = 500,
):
    try:
        result = operations.task_events(get_instance(instance_id), task_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{instance_id}/tasks/{task_id}/flow")
def bot_task_flow(
    instance_id: str,
    task_id: str,
    response: Response,
):
    try:
        result = operations.task_flow(get_instance(instance_id), task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="task not found")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.get("/{instance_id}/tasks/{task_id}/contexts/{snapshot_id}")
def bot_task_context(
    instance_id: str,
    task_id: str,
    snapshot_id: str,
    response: Response,
):
    try:
        result = observability.context_snapshot(
            get_instance(instance_id),
            task_id,
            snapshot_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except observability.UnsafeContextSnapshotError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="context snapshot not found")
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/{instance_id}/sync")
def bot_sync(request: Request, instance_id: str, dry_run: bool = False, restart: bool = True):
    inst = get_instance(instance_id)
    return start_task(
        request,
        inst,
        "sync",
        lambda: operations.stream_sync(inst, dry_run=dry_run, restart_after=restart),
    )


@router.post("/{instance_id}/rebuild")
def bot_rebuild(request: Request, instance_id: str, restart: bool = True):
    inst = get_instance(instance_id)
    return start_task(request, inst, "rebuild", lambda: operations.stream_rebuild(inst, restart_after=restart))


@router.post("/{instance_id}/update")
def bot_update(request: Request, instance_id: str, dry_run: bool = False):
    inst = get_instance(instance_id)
    return start_task(request, inst, "update", lambda: operations.stream_update(inst, dry_run=dry_run))


@router.post("/{instance_id}/dump")
def bot_dump(request: Request, instance_id: str, mode: str = "quick"):
    inst = get_instance(instance_id)
    return start_task(request, inst, "dump", lambda: operations.stream_dump(inst, mode=mode))


@router.post("/{instance_id}/register")
def bot_register(request: Request, instance_id: str):
    inst = get_instance(instance_id)
    return start_task(request, inst, "register", lambda: operations.stream_register(inst))


@router.post("/{instance_id}/setup-actions/{action_id}")
def bot_setup_action(request: Request, instance_id: str, action_id: str, verb: str = "start"):
    inst = get_instance(instance_id)
    return start_task(
        request,
        inst,
        f"setup-action:{action_id}:{verb}",
        lambda: operations.stream_setup_action(inst, action_id, verb),
    )


@router.get("/{instance_id}/provision/schema")
def bot_provision_schema(instance_id: str):
    return operations.provision_schema(get_instance(instance_id))


@router.post("/{instance_id}/provision/env")
def bot_provision_env(instance_id: str, body: dict[str, Any]):
    inst = get_instance(instance_id)
    secrets = {str(key): "" if value is None else str(value) for key, value in body.items()}
    res = operations.write_instance_env(inst, secrets)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "write failed")
    return res


def _bot_yaml_path(inst) -> Path:
    raw = Path(inst.bot_spec)
    return raw if raw.is_absolute() else repo_root() / raw


@router.get("/{instance_id}/tools")
def bot_tools(instance_id: str):
    inst = get_instance(instance_id)
    return catalog.bot_tool_config(_bot_yaml_path(inst))


@router.put("/{instance_id}/tools")
def update_bot_tools(
    request: Request,
    instance_id: str,
    body: dict[str, Any],
    apply: bool = False,
):
    from console.control.yaml_editor import apply_tool_config

    inst = get_instance(instance_id)
    bot_yaml = _bot_yaml_path(inst)
    tools = body.get("tools") if isinstance(body.get("tools"), dict) else {}
    agents = body.get("agents") if isinstance(body.get("agents"), dict) else {}
    mcp = tools.get("mcp") if isinstance(tools.get("mcp"), dict) else {}

    def _apply_config() -> dict[str, Any]:
        return apply_tool_config(
            bot_yaml,
            tool_packs=tools.get("packs", []),
            tool_features=tools.get("features", []),
            hidden_tools=tools.get("hide", []),
            mcp_servers=mcp.get("servers") if isinstance(mcp, dict) and "servers" in mcp else None,
            agent_presets=agents.get("presets", []),
            workflows=agents.get("workflows", []),
        )

    if apply:
        def _stream_apply() -> Iterator[str]:
            try:
                result = _apply_config()
            except Exception as exc:  # noqa: BLE001 - task boundary reports write failures in task logs
                yield f"[ERR] 工具配置写入失败：{exc}"
                yield "__EXIT__ 1"
                return
            for warning in result.get("warnings", []):
                yield f"[WARN] {warning}"
            if not result.get("ok"):
                yield f"[ERR] 工具配置写入失败：{result.get('error', 'update failed')}"
                yield "__EXIT__ 1"
                return
            yield "[console] 工具配置已写入源仓，正在更新运行实例..."
            yield from operations.stream_update(inst)

        return start_task(request, inst, "apply-tools", _stream_apply)

    result = _apply_config()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "update failed"))
    result["restart_required"] = True
    result["apply_required"] = True
    return result


@router.get("/{instance_id}/logs/stream")
def stream_logs(instance_id: str, source: str = "cc", lines: int = 200):
    inst = get_instance(instance_id)
    paths = operations.resolve_log_files(inst, source)
    if not paths:
        raise HTTPException(status_code=404, detail="no log file is available for this instance")
    return sse(operations.follow_log(paths[0], from_end_lines=lines))


@router.post("/{instance_id}/{verb}")
def bot_control(instance_id: str, verb: str):
    if verb not in {"start", "stop", "restart"}:
        raise HTTPException(status_code=404, detail=f"unknown action: {verb}")
    res = operations.control(get_instance(instance_id), verb)
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or res.get("stderr") or "action failed")
    return res
