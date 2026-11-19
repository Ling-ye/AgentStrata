"""Application-scoped observation and user settings endpoints."""
from fastapi import Request, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from console.backend.routes.applications import router, service
from console.control.application_management import Management
from console.control.native_applications import NativeApplications


class SettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(min_length=64, max_length=64)
    values: dict[str, str]


def invoke(operation):
    from chatcopilot.core.application_settings import SettingsConflict
    try:
        return operation()
    except ConnectionError:
        raise HTTPException(503, "桌宠连接暂不可用，请确认客户端后台在线后重试。") from None
    except SettingsConflict:
        raise HTTPException(409, "配置已被更新，请重新读取后保存。") from None
    except KeyError:
        raise HTTPException(404, "应用或记录不存在") from None
    except PermissionError:
        raise HTTPException(403, "此应用尚不支持该操作") from None
    except ValueError:
        raise HTTPException(400, "应用配置或查询参数无效，请重新核对。") from None
    except (OSError, RuntimeError):
        raise HTTPException(503, "应用记录或配置暂不可用，请稍后重试。") from None


@router.post("/connections/native")
def connect_native(request: Request):
    return invoke(lambda: NativeApplications(service(request).state_dir).connect())


@router.get("/{application_id}/settings")
def settings(request: Request, application_id: str):
    return invoke(lambda: Management(service(request)).read(application_id, "settings"))


@router.post("/{application_id}/settings/{section}")
def save_settings(request: Request, application_id: str, section: str, body: SettingsWrite):
    return invoke(lambda: Management(service(request)).write(application_id, section, body.revision, body.values))


@router.get("/{application_id}/observations/source")
def source(request: Request, application_id: str):
    return invoke(lambda: Management(service(request)).source(application_id))


@router.get("/{application_id}/observations/{path:path}")
def observation(request: Request, application_id: str, path: str):
    parts = path.split("/")
    args = dict(request.query_params)
    try:
        for key in ("after", "limit", "page", "event_seq"):
            if key in args:
                args[key] = int(args[key])
        for key in ("since", "until", "min_ms"):
            if key in args:
                args[key] = float(args[key])
    except ValueError:
        raise HTTPException(400, "查询参数无效") from None
    if parts == ["inspection"]:
        operation = "inspection"
    elif parts == ["gateway-observation"]:
        operation = "history"
    elif parts == ["gateway-observation", "metrics"]:
        operation = "metrics"
    elif len(parts) >= 3 and parts[:2] == ["gateway-observation", "runs"]:
        args["run_id"] = parts[2]
        tail = parts[3:]
        if not tail:
            operation = "run"
        elif tail == ["events"]:
            operation = "events"
        elif len(tail) == 2 and tail[0] == "details":
            operation = "body"
            args["body_id"] = tail[1]
        elif tail == ["trace"] or len(tail) == 3 and tail[:2] == ["trace", "steps"]:
            operation = "trace"
            if len(tail) == 3:
                args["span_id"] = tail[2]
        else:
            raise HTTPException(404, "记录入口不存在")
    else:
        raise HTTPException(404, "记录入口不存在")
    result = invoke(lambda: Management(service(request)).read(application_id, operation, args))
    if result is None and "run_id" in args:
        raise HTTPException(404, "当前应用没有该任务记录")
    return result
