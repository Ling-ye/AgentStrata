"""QQ OneBot configuration validation and authenticated health probes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Any, Mapping

from chatcopilot.platforms.base import (
    ExternalCheckItem,
    ExternalCheckReport,
    ExternalCheckStatus,
    ExternalCheckVerdict,
)
from chatcopilot.platforms.qq.boundary import (
    QQBoundaryError,
    require_access_token,
    require_loopback_websocket_url,
)


_QQ_ID_RE = re.compile(r"^[1-9][0-9]{4,19}$")
_ENV_GROUP_ID = "CHATCOPILOT_EXTERNAL_CHECK_QQ_GROUP_ID"
_MAX_FRAME_BYTES = 256 * 1024
_MAX_FRAMES = 8


@dataclass(frozen=True)
class OneBotRuntimeStatus:
    online: bool
    good: bool


def _runtime_status_from_response(response: Mapping[str, Any]) -> OneBotRuntimeStatus:
    data = response.get("data")
    if not isinstance(data, Mapping):
        raise RuntimeError("OneBot get_status returned no status data")
    online = data.get("online")
    good = data.get("good")
    if not isinstance(online, bool) or not isinstance(good, bool):
        raise RuntimeError("OneBot get_status returned invalid status flags")
    return OneBotRuntimeStatus(online=online, good=good)


async def _onebot_action(
    url: str,
    token: str | None,
    *,
    action: str,
    params: Mapping[str, Any],
    echo: str,
) -> Mapping[str, Any]:
    import websockets

    headers = {"Authorization": f"Bearer {token}"} if token else None
    connection_options = {
        "open_timeout": 3,
        "close_timeout": 1,
        "max_size": _MAX_FRAME_BYTES,
    }
    kwargs = (
        {"additional_headers": headers, **connection_options} if headers else connection_options
    )
    try:
        try:
            connection = await websockets.connect(url, **kwargs)
        except TypeError:
            legacy_kwargs = (
                {"extra_headers": headers, **connection_options} if headers else connection_options
            )
            connection = await websockets.connect(url, **legacy_kwargs)
        await connection.send(
            json.dumps(
                {"action": action, "params": dict(params), "echo": echo},
                separators=(",", ":"),
            )
        )
        for _ in range(_MAX_FRAMES):
            raw = await asyncio.wait_for(connection.recv(), timeout=3)
            response = json.loads(raw)
            if not isinstance(response, dict):
                continue
            try:
                retcode = int(str(response.get("retcode")))
            except (TypeError, ValueError):
                retcode = -1
            if response.get("status") == "failed" and retcode == 1403:
                raise PermissionError("OneBot rejected the access token")
            if response.get("echo") != echo:
                continue
            if response.get("status") != "ok" or retcode != 0:
                raise RuntimeError("OneBot probe action was rejected")
            return response
        raise RuntimeError("OneBot probe response did not match the request")
    finally:
        connection_value = locals().get("connection")
        if connection_value is not None:
            await connection_value.close()


async def _connect_once(
    url: str,
    token: str | None,
) -> OneBotRuntimeStatus:
    response = await _onebot_action(
        url,
        token,
        action="get_status",
        params={},
        echo="chatcopilot-onebot-auth-probe",
    )
    return _runtime_status_from_response(response)


async def query_onebot_runtime_status(url: str, token: str) -> OneBotRuntimeStatus:
    """Read authenticated provider and QQ account status without testing the auth boundary."""

    return await _connect_once(url, token)


async def probe_onebot_boundary(url: str, token: str) -> OneBotRuntimeStatus:
    """Require unauthenticated rejection and authenticated connection success."""
    unauthenticated_rejected = False
    try:
        await _connect_once(url, None)
    except Exception:  # noqa: BLE001 - every handshake/close rejection is acceptable here
        unauthenticated_rejected = True
    if not unauthenticated_rejected:
        raise QQBoundaryError(
            "qq_onebot_accepts_unauthenticated",
            "OneBot rejected boundary check: unauthenticated WebSocket was accepted",
        )
    try:
        return await _connect_once(url, token)
    except Exception as exc:  # noqa: BLE001 - normalize library/network errors
        raise QQBoundaryError(
            "qq_onebot_authenticated_probe_failed",
            f"authenticated OneBot WebSocket probe failed ({type(exc).__name__})",
        ) from exc


async def probe_onebot_online(url: str, token: str) -> OneBotRuntimeStatus:
    """Require the authenticated OneBot boundary and an online, healthy QQ account."""

    status = await probe_onebot_boundary(url, token)
    if not status.online:
        raise QQBoundaryError(
            "qq_account_offline",
            "OneBot is reachable but the QQ account is offline",
        )
    if not status.good:
        raise QQBoundaryError(
            "qq_onebot_unhealthy",
            "OneBot reports an unhealthy QQ provider state",
        )
    return status


def _require_qq_id(value: str | None, *, env_key: str) -> str:
    normalized = str(value or "").strip()
    if _QQ_ID_RE.fullmatch(normalized) is None:
        raise QQBoundaryError(
            "qq_identity_invalid",
            f"{env_key} must be a numeric QQ ID",
        )
    return normalized


def _identity_hmac(token: str, *, namespace: str, value: object) -> str:
    return hmac.new(
        token.encode("utf-8"),
        f"{namespace}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:16]


def _check_verdict(checks: list[ExternalCheckItem]) -> ExternalCheckVerdict:
    required = [item for item in checks if item.required]
    if any(item.status == "error" for item in required):
        return "error"
    if any(item.status == "failed" for item in required):
        return "failed"
    if any(item.status != "passed" for item in required):
        return "error"
    return "passed"


async def run_qq_external_checks(
    env: Mapping[str, str],
    *,
    bot_id: str,
    send_message: bool = False,
    confirm_external_write: bool = False,
) -> ExternalCheckReport:
    """Check the configured QQ gateway without creating an Agent Evaluation."""

    checks: list[ExternalCheckItem] = []
    limitations = [
        "没有独立发送端；外部用户入站消息、Agent 处理和 QQ 回复链路未测试。",
        "OneBot 发送回执只证明动作被接受，不证明群成员实际看到消息。",
        "模拟 ingress 只验证本地 hermetic gateway relay，不证明真实 NapCat 或 Agent 入站。",
    ]
    attempted = False
    performed = False

    if send_message != confirm_external_write:
        checks.append(
            ExternalCheckItem(
                check_id="external_write_confirmation",
                label="一次性外部写确认",
                status="failed",
                required=True,
                detail="--send-message 与 --confirm-external-write 必须同时提供",
            )
        )
        checks.append(_inbound_not_tested())
        return ExternalCheckReport(
            platform="qq",
            bot_id=bot_id,
            verdict="failed",
            checks=tuple(checks),
            limitations=tuple(limitations),
        )

    try:
        url = require_loopback_websocket_url(
            env.get("CHATCOPILOT_QQ_ONEBOT_WS_URL") or "ws://127.0.0.1:3001",
            env_key="CHATCOPILOT_QQ_ONEBOT_WS_URL",
        )
        token = require_access_token(env.get("QQ_ACCESS_TOKEN"))
        account = _require_qq_id(env.get("QQ_ACCOUNT"), env_key="QQ_ACCOUNT")
    except QQBoundaryError as exc:
        checks.append(
            ExternalCheckItem(
                check_id="qq_configuration",
                label="QQ 外部检查配置",
                status="failed",
                required=True,
                detail=exc.error_code,
            )
        )
        checks.append(_inbound_not_tested())
        return ExternalCheckReport(
            platform="qq",
            bot_id=bot_id,
            verdict="failed",
            checks=tuple(checks),
            limitations=tuple(limitations),
        )

    checks.append(
        ExternalCheckItem(
            check_id="qq_configuration",
            label="QQ 外部检查配置",
            status="passed",
            required=True,
            detail="回环 OneBot URL、强 token 与 Bot QQ ID 已配置",
        )
    )

    boundary_passed = False
    runtime_status: OneBotRuntimeStatus | None = None
    try:
        runtime_status = await probe_onebot_boundary(url, token)
    except QQBoundaryError as exc:
        status: ExternalCheckStatus = (
            "failed" if exc.error_code == "qq_onebot_accepts_unauthenticated" else "error"
        )
        checks.append(
            ExternalCheckItem(
                check_id="onebot_boundary",
                label="OneBot 认证边界",
                status=status,
                required=True,
                detail=exc.error_code,
            )
        )
    else:
        boundary_passed = True
        checks.append(
            ExternalCheckItem(
                check_id="onebot_boundary",
                label="OneBot 认证边界",
                status="passed",
                required=True,
                detail="未认证连接被拒绝，认证 get_status 成功",
            )
        )

    account_ready = bool(
        boundary_passed
        and runtime_status is not None
        and runtime_status.online
        and runtime_status.good
    )
    if boundary_passed and runtime_status is not None:
        if not runtime_status.online:
            account_detail = "QQ 账号离线"
        elif not runtime_status.good:
            account_detail = "QQ 账号在线，但 OneBot 报告运行状态异常"
        else:
            account_detail = "QQ 账号在线且 OneBot 状态正常"
        checks.append(
            ExternalCheckItem(
                check_id="qq_account_online",
                label="QQ 在线状态",
                status="passed" if account_ready else "failed",
                required=True,
                detail=account_detail,
                evidence={
                    "online": runtime_status.online,
                    "good": runtime_status.good,
                },
            )
        )
    else:
        checks.append(
            ExternalCheckItem(
                check_id="qq_account_online",
                label="QQ 在线状态",
                status="not_tested",
                required=True,
                detail="OneBot 认证边界未通过，未确认 QQ 在线状态",
            )
        )

    if account_ready:
        try:
            response = await _onebot_action(
                url,
                token,
                action="get_login_info",
                params={},
                echo="chatcopilot-external-login-info",
            )
            data = response.get("data")
            actual = str(data.get("user_id") if isinstance(data, dict) else "").strip()
            matches = actual == account
            checks.append(
                ExternalCheckItem(
                    check_id="qq_login_identity",
                    label="QQ 登录账号",
                    status="passed" if matches else "failed",
                    required=True,
                    detail="登录账号与 Bot 配置一致" if matches else "登录账号与 Bot 配置不一致",
                    evidence={
                        "configured_account_hmac": _identity_hmac(
                            token,
                            namespace="qq-account",
                            value=account,
                        ),
                        "observed_account_hmac": _identity_hmac(
                            token,
                            namespace="qq-account",
                            value=actual or "missing",
                        ),
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - normalize transport/protocol failures
            checks.append(
                ExternalCheckItem(
                    check_id="qq_login_identity",
                    label="QQ 登录账号",
                    status="error",
                    required=True,
                    detail=f"get_login_info failed ({type(exc).__name__})",
                )
            )
    else:
        if not boundary_passed:
            identity_detail = "OneBot 认证边界未通过，未查询登录账号"
        else:
            identity_detail = "QQ 账号未就绪，未查询登录账号"
        checks.append(
            ExternalCheckItem(
                check_id="qq_login_identity",
                label="QQ 登录账号",
                status="not_tested",
                required=True,
                detail=identity_detail,
            )
        )

    group_raw = str(env.get(_ENV_GROUP_ID) or "").strip()
    group = ""
    if group_raw:
        try:
            group = _require_qq_id(group_raw, env_key=_ENV_GROUP_ID)
        except QQBoundaryError as exc:
            checks.append(
                ExternalCheckItem(
                    check_id="qq_group_access",
                    label="QQ 群访问",
                    status="failed",
                    required=True,
                    detail=exc.error_code,
                )
            )
        else:
            if not account_ready:
                checks.append(
                    ExternalCheckItem(
                        check_id="qq_group_access",
                        label="QQ 群访问",
                        status="not_tested",
                        required=send_message,
                        detail="QQ 账号未就绪，未继续访问检查群",
                    )
                )
            else:
                try:
                    response = await _onebot_action(
                        url,
                        token,
                        action="get_group_info",
                        params={"group_id": int(group), "no_cache": True},
                        echo="chatcopilot-external-group-info",
                    )
                    data = response.get("data")
                    actual = str(data.get("group_id") if isinstance(data, dict) else "").strip()
                    matches = actual == group
                    checks.append(
                        ExternalCheckItem(
                            check_id="qq_group_access",
                            label="QQ 群访问",
                            status="passed" if matches else "failed",
                            required=True,
                            detail="Bot 可访问固定检查群" if matches else "OneBot 返回了非预期群身份",
                            evidence={
                                "configured_group_hmac": _identity_hmac(
                                    token,
                                    namespace="qq-group",
                                    value=group,
                                ),
                                "observed_group_hmac": _identity_hmac(
                                    token,
                                    namespace="qq-group",
                                    value=actual or "missing",
                                ),
                            },
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    checks.append(
                        ExternalCheckItem(
                            check_id="qq_group_access",
                            label="QQ 群访问",
                            status="error",
                            required=True,
                            detail=f"get_group_info failed ({type(exc).__name__})",
                        )
                    )
    else:
        checks.append(
            ExternalCheckItem(
                check_id="qq_group_access",
                label="QQ 群访问",
                status="not_configured",
                required=send_message,
                detail=f"未配置 {_ENV_GROUP_ID}",
            )
        )

    checks.append(await _simulated_gateway_ingress_check(env))

    if send_message:
        if not group:
            checks.append(
                ExternalCheckItem(
                    check_id="qq_outbound_message",
                    label="QQ 群出站消息",
                    status="failed",
                    required=True,
                    detail="发送探针需要固定检查群",
                )
            )
        elif any(item.required and item.status != "passed" for item in checks):
            checks.append(
                ExternalCheckItem(
                    check_id="qq_outbound_message",
                    label="QQ 群出站消息",
                    status="not_tested",
                    required=True,
                    detail="前置检查未通过，未尝试外部写",
                )
            )
        else:
            nonce = secrets.token_hex(8)
            attempted = True
            try:
                response = await _onebot_action(
                    url,
                    token,
                    action="send_group_msg",
                    params={
                        "group_id": int(group),
                        "message": f"[AgentStrata external check] nonce={nonce}",
                    },
                    echo="chatcopilot-external-send-group",
                )
                data = response.get("data")
                message_id = data.get("message_id") if isinstance(data, dict) else None
                valid_receipt = message_id not in {None, ""}
                performed = valid_receipt
                checks.append(
                    ExternalCheckItem(
                        check_id="qq_outbound_message",
                        label="QQ 群出站消息",
                        status="passed" if valid_receipt else "failed",
                        required=True,
                        detail=(
                            "OneBot 接受固定 nonce 群消息动作"
                            if valid_receipt
                            else "OneBot 未返回有效消息回执"
                        ),
                        evidence={
                            "nonce": nonce,
                            "message_hmac": _identity_hmac(
                                token,
                                namespace="qq-message",
                                value=message_id or "missing",
                            ),
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                limitations.append("发送动作异常后无法证明消息一定未送达。")
                checks.append(
                    ExternalCheckItem(
                        check_id="qq_outbound_message",
                        label="QQ 群出站消息",
                        status="error",
                        required=True,
                        detail=f"send_group_msg failed ({type(exc).__name__})",
                    )
                )

    checks.append(_inbound_not_tested())
    return ExternalCheckReport(
        platform="qq",
        bot_id=bot_id,
        verdict=_check_verdict(checks),
        checks=tuple(checks),
        external_write_attempted=attempted,
        external_write_performed=performed,
        limitations=tuple(limitations),
    )


async def _simulated_gateway_ingress_check(
    env: Mapping[str, str],
) -> ExternalCheckItem:
    """Run a hermetic relay probe without exposing a production injection API."""

    from chatcopilot.platforms.qq.ingress_probe import (
        run_simulated_gateway_ingress,
    )

    try:
        receipt = await run_simulated_gateway_ingress(env)
    except Exception as exc:  # noqa: BLE001 - normalize local transport/protocol failures
        return ExternalCheckItem(
            check_id="qq_simulated_gateway_ingress",
            label="QQ gateway 模拟入站",
            status="error",
            required=True,
            detail=f"hermetic gateway ingress failed ({type(exc).__name__})",
        )
    return ExternalCheckItem(
        check_id="qq_simulated_gateway_ingress",
        label="QQ gateway 模拟入站",
        status="passed" if receipt.passed else "failed",
        required=True,
        detail=(
            "合成 OneBot 正例已转发且负例已丢弃"
            if receipt.passed
            else "gateway relay 未形成完整的正例转发/负例丢弃证据"
        ),
        evidence=receipt.to_evidence(),
    )


def _inbound_not_tested() -> ExternalCheckItem:
    return ExternalCheckItem(
        check_id="qq_inbound_agent_roundtrip",
        label="QQ 入站 Agent 往返",
        status="not_tested",
        required=False,
        detail="缺少独立发送 QQ，不能执行入站端到端验证",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate/probe the QQ OneBot boundary")
    parser.add_argument("action", choices=("validate-url", "validate", "probe", "online"))
    parser.add_argument("--url", required=True)
    parser.add_argument("--url-env-key", default="CHATCOPILOT_QQ_ONEBOT_WS_URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        url = require_loopback_websocket_url(args.url, env_key=args.url_env_key)
        if args.action == "validate-url":
            print(f"[OK] QQ OneBot loopback URL valid; env_key={args.url_env_key}")
            return 0
        token = require_access_token(os.environ.get("QQ_ACCESS_TOKEN"))
        if args.action == "probe":
            asyncio.run(probe_onebot_boundary(url, token))
        elif args.action == "online":
            asyncio.run(probe_onebot_online(url, token))
    except QQBoundaryError as exc:
        print(f"[ERR] {exc.error_code}: {exc}")
        return 1
    if args.action == "probe":
        action = "boundary-probe-ok"
    elif args.action == "online":
        action = "account-online-ok"
    else:
        action = "config-ok"
    print(f"[OK] QQ OneBot {action}; token_length={len(token)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "OneBotRuntimeStatus",
    "QQBoundaryError",
    "_onebot_action",
    "main",
    "probe_onebot_boundary",
    "probe_onebot_online",
    "query_onebot_runtime_status",
    "require_access_token",
    "require_loopback_websocket_url",
    "run_qq_external_checks",
]
