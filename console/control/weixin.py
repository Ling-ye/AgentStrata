"""Console-facing binding control, using host configuration and instance leases."""

from __future__ import annotations

from pathlib import Path
import shlex
import threading
from dataclasses import replace

from chatcopilot.botspec.channel_configuration import configuration_adapter
from chatcopilot.botspec.deployment_env import (
    deployment_environment,
    exported_environment,
    runtime_environment_keys,
)
from chatcopilot.botspec.loader import load_botspec
from chatcopilot.botspec.provisioning import (
    build_provision_plan,
    patch_local_env,
    read_local_env_for_provision,
    write_private_env_text,
)
from chatcopilot.channels.weixin_ilink.login import WeixinLoginService
from chatcopilot.channels.weixin_ilink.client import WeixinClient
from chatcopilot.contracts.weixin import WeixinError
from chatcopilot.contracts.gateway_rpc import HealthParams
from chatcopilot.core.private_sqlite import private_directory, private_lock
from chatcopilot.protocols.gateway_client import GatewayClientConfig, GatewayWebSocketClient
from console.control.discovery import repo_root
from console.control import operations


class WeixinControl:
    def __init__(self, *, login_service=None):
        self.login = login_service or WeixinLoginService(client_factory=WeixinClient)

    def _configuration(self, instance):
        path = Path(instance.bot_spec)
        if not path.is_absolute():
            path = repo_root() / path
        expected = repo_root().resolve() / "bots" / instance.instance_id / "bot.yaml"
        if path.is_symlink() or path.parent.is_symlink() or path.resolve() != expected:
            raise WeixinError("weixin_configuration_path_invalid")
        spec = load_botspec(path)
        if spec.id != instance.instance_id or spec.channels.weixin is None or spec.gateway is None:
            raise WeixinError("weixin_instance_required")
        local = path.parent / "local.env"
        values = read_local_env_for_provision(local, allowed_parent=local.parent)
        runtime = deployment_environment(spec, values, source_root=repo_root(), home=Path.home())
        return spec, local, values, runtime

    async def status(self, instance) -> dict:
        spec, _, values, runtime = self._configuration(instance)
        channel = spec.channels.weixin
        bound = all(
            values.get(key)
            for key in (channel.access_token_env, channel.account_env, channel.user_env)
        )
        state = operations.status(instance, include_services=False)
        connected, connection_state = False, "stopped"
        if state.get("running"):
            connection_state = "unknown"
            token = runtime.get(spec.gateway.token_env)
            if token:
                client = GatewayWebSocketClient(
                    GatewayClientConfig(
                        url=runtime["CHATCOPILOT_GATEWAY_URL"],
                        token=token,
                        connect_timeout_seconds=3,
                        request_timeout_seconds=3,
                        close_timeout_seconds=1,
                    )
                )
                try:
                    await client.connect()
                    health = await client.request("health", HealthParams())
                    connected = health.ready
                    connection_state = "connected" if connected else "error"
                except Exception:
                    pass
                finally:
                    await client.close()
        return {
            "bound": bool(bound),
            "account_id": values.get(channel.account_env),
            "user_id": values.get(channel.user_env),
            "connected": connected,
            "connection_state": connection_state,
            "login_active": self.login.active(instance.instance_id),
        }

    def start(self, instance, task_manager) -> dict:
        spec, local, initial, runtime = self._configuration(instance)
        if operations.status(instance, include_services=False).get("running"):
            raise WeixinError("weixin_instance_running")
        if self.login.active(instance.instance_id) or task_manager.active_for(instance.instance_id):
            raise WeixinError("weixin_login_active")
        state_root = private_directory(Path(runtime[spec.gateway.state_root_env]))
        lease = private_lock(state_root / "gateway.instance.lock")
        try:
            lease.__enter__()
        except (OSError, TimeoutError, ValueError):
            raise WeixinError("weixin_instance_busy") from None
        done = threading.Event()

        def reservation():
            yield "微信绑定操作进行中"
            done.wait()
            yield "__EXIT__ 0"

        def save(binding):
            current = read_local_env_for_provision(local, allowed_parent=local.parent)
            channel = spec.channels.weixin
            for env_key, result_key in (
                (channel.account_env, "ilink_bot_id"),
                (channel.user_env, "ilink_user_id"),
            ):
                if current.get(env_key) and current[env_key] != binding[result_key]:
                    raise WeixinError("weixin_account_replacement_requires_new_instance")
            if operations.status(instance, include_services=False).get("running"):
                raise WeixinError("weixin_instance_running")
            owners = list(
                dict.fromkeys(
                    [
                        *(
                            value.strip()
                            for value in current.get("CHATCOPILOT_ADD_OWNER_IDS", "").split(",")
                            if value.strip()
                        ),
                        binding["ilink_user_id"],
                    ]
                )
            )
            adapter = configuration_adapter(spec)
            plan = build_provision_plan(spec, adapter, current)
            binding_fields = {
                channel.access_token_env,
                channel.account_env,
                channel.user_env,
                channel.endpoint_env,
                spec.gateway.port_env,
                spec.gateway.token_env,
                spec.gateway.state_root_env,
                "CHATCOPILOT_ADD_OWNER_IDS",
            }
            plan = replace(
                plan,
                fields=tuple(field for field in plan.fields if field.env_key in binding_fields),
            )
            patch_local_env(
                local,
                plan,
                {
                    channel.access_token_env: binding["bot_token"],
                    channel.account_env: binding["ilink_bot_id"],
                    channel.user_env: binding["ilink_user_id"],
                    channel.endpoint_env: binding["baseurl"],
                    "CHATCOPILOT_ADD_OWNER_IDS": ",".join(owners),
                },
                adapter=adapter,
                allowed_parent=local.parent,
            )
            if instance.is_deployed:
                saved = read_local_env_for_provision(local, allowed_parent=local.parent)
                environment = deployment_environment(
                    spec, saved, source_root=repo_root(), home=Path.home()
                )
                exported = exported_environment(
                    environment, runtime_environment_keys(spec, environment=environment)
                )
                target = Path(environment["CHATCOPILOT_ENV_FILE"])
                text = (
                    "\n".join(
                        f"export {key}={shlex.quote(value)}" for key, value in exported.items()
                    )
                    + "\n"
                )
                write_private_env_text(target, text, allowed_parent=target.parent)

        def finish():
            lease.__exit__(None, None, None)
            done.set()

        try:
            task_manager.start(instance.instance_id, "weixin-binding", reservation)
            token = initial.get(spec.channels.weixin.access_token_env)
            return self.login.start(
                instance.instance_id, save, finish=finish, local_tokens=(token,) if token else ()
            )
        except BaseException:
            finish()
            raise

    async def close(self):
        await self.login.close()
