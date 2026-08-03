"""Shared lark-cli (Feishu) subprocess runner core.

This module owns the *mechanism* of driving the official ``lark-cli`` as the
application/bot identity (``--as bot``): binary/node resolution, isolated HOME,
auth/permission error classification, OpenAPI response checks, and a generic
``run_api`` helper. It is intentionally domain-agnostic so multiple external
tool domains can reuse the same process plumbing instead of duplicating it.

Bot identity only needs ``App ID`` + ``App Secret`` (tenant access token); it does
**not** require any user OAuth scope. The prerequisite is that the target
document/sheet/bitable is shared with the application as a collaborator.

Dependencies: standard library only. Must not import ``chatcopilot.middleware.*``
or ``chatcopilot.platforms.*`` (external tools layer constraint).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen


# ----------------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------------
class LarkCliError(Exception):
    """lark-cli 调用异常"""


class LarkCliNotFoundError(LarkCliError):
    """lark-cli 未安装"""


class LarkCliAuthError(LarkCliError):
    """lark-cli 未认证 / 权限不足"""


# ----------------------------------------------------------------------------
# Error classification
# ----------------------------------------------------------------------------
def combine_cli_output(result: subprocess.CompletedProcess) -> str:
    """合并 stdout/stderr，便于统一错误识别。"""
    return ((result.stderr or "").strip() + "\n" + (result.stdout or "").strip()).strip()


def is_auth_error_text(output_lower: str) -> bool:
    """判断是否为鉴权相关错误（含缺少 access token）。"""
    return any(
        kw in output_lower
        for kw in (
            "missing access token",
            "code\":99991661",
            "code:99991661",
            "unauthorized",
            "access token",
            "auth",
            "login",
        )
    )


def extract_troubleshooter_url(output: str) -> str:
    """从错误输出中提取飞书 troubleshooting 链接。"""
    m = re.search(r"https://open\.feishu\.cn/search\?[^\s\"']+", output)
    return m.group(0) if m else ""


def raise_lark_cli_error_for_output(output: str) -> None:
    """按输出内容抛出更明确的异常。"""
    output_lower = output.lower()
    if is_auth_error_text(output_lower):
        ts_url = extract_troubleshooter_url(output)
        tips = (
            "lark-cli 鉴权失败（可能缺少 access token）。\n"
            "请先执行:\n"
            "  lark-cli config show\n"
            "  lark-cli config init --new\n"
            "并确认目标文档已共享给该应用。"
        )
        if ts_url:
            tips += f"\n排查链接: {ts_url}"
        raise LarkCliAuthError(f"{tips}\n原始错误: {output}")
    if "permission" in output_lower or "scope" in output_lower:
        raise LarkCliAuthError(
            "lark-cli 应用权限不足。请在飞书开发者后台开通所需权限，"
            "并将目标文档共享给该应用。\n"
            f"原始错误: {output}"
        )
    raise LarkCliError(f"lark-cli 命令失败:\n{output}")


def raise_if_api_error(payload: Dict[str, Any], context: str) -> None:
    """检查 OpenAPI JSON 响应中的 code 字段并在失败时抛错。"""
    if not isinstance(payload, dict):
        return
    code = payload.get("code")
    if code in (None, 0):
        return

    msg = str(payload.get("msg", "")).strip()
    error_obj = payload.get("error")
    details = f"{context} 失败: code={code}, msg={msg}"
    if error_obj:
        try:
            details = f"{details}, error={json.dumps(error_obj, ensure_ascii=False)}"
        except Exception:
            details = f"{details}, error={error_obj}"
    raise_lark_cli_error_for_output(details)


# ----------------------------------------------------------------------------
# Isolated HOME + subprocess env
# ----------------------------------------------------------------------------
_LARK_HOME_CACHE: Optional[str] = None


def resolve_lark_home() -> Optional[str]:
    """返回应用专属的 lark-cli HOME 目录（含 .lark-cli/config.json）。

    独立运行时：使用 <程序目录>/runtime/lark_home，让 lark-cli 完全
    脱离用户原本的 ~/.lark-cli/，零污染、开箱即用。

    开发期：如果项目根 dist/runtime/lark_home/.lark-cli/config.json 存在
    则用它（方便本地复现隔离 HOME 行为），否则返回 None 走系统默认 HOME。
    """
    global _LARK_HOME_CACHE
    if _LARK_HOME_CACHE is not None:
        return _LARK_HOME_CACHE or None

    candidates: List[Path] = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "runtime" / "lark_home")
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "tools" / "database").is_dir() and (parent / "agent").is_dir():
            candidates.append(parent / "dist" / "runtime" / "lark_home")
            break

    for c in candidates:
        if (c / ".lark-cli" / "config.json").is_file():
            _LARK_HOME_CACHE = str(c)
            return _LARK_HOME_CACHE

    _LARK_HOME_CACHE = ""
    return None


def lark_subprocess_env() -> Dict[str, str]:
    """返回供 subprocess 使用的环境变量副本，HOME / USERPROFILE 指向应用专属目录。

    Windows 上 lark-cli 解析 ~ 时优先使用 USERPROFILE，所以两者都要覆盖。
    """
    env = os.environ.copy()
    home = resolve_lark_home()
    if home:
        env["HOME"] = home
        env["USERPROFILE"] = home
    _prepend_wsl_user_bins(env)
    return env


def _prepend_wsl_user_bins(env: Dict[str, str]) -> None:
    """WSL 运行时 HOME 可能被改到实例目录，仍应优先使用真实用户安装的 CLI。"""
    candidates: List[str] = []
    user = env.get("USER") or env.get("LOGNAME")
    if user:
        candidates.append(f"/home/{user}/.npm-global/bin")
        candidates.append(f"/home/{user}/.local/bin")
    candidates.extend([
        str(Path.home() / ".npm-global" / "bin"),
        str(Path.home() / ".local" / "bin"),
    ])

    path_parts = [p for p in str(env.get("PATH") or "").split(os.pathsep) if p]
    for candidate in reversed(candidates):
        if os.path.isdir(candidate) and candidate not in path_parts:
            path_parts.insert(0, candidate)
    env["PATH"] = os.pathsep.join(path_parts)


def subprocess_run(cmd, **kwargs):
    """统一的 subprocess.run 封装，Windows 上使用 shell=True 并强制 UTF-8 编码。"""
    kwargs.setdefault("shell", True)
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    kwargs.setdefault("env", lark_subprocess_env())
    return subprocess.run(cmd, **kwargs)


# ----------------------------------------------------------------------------
# Installation / credential checks
# ----------------------------------------------------------------------------
def check_lark_cli_installed() -> bool:
    """检测 lark-cli 是否已安装"""
    try:
        result = subprocess_run("lark-cli --version", timeout=10)
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except subprocess.TimeoutExpired:
        return False


def check_lark_cli_auth() -> bool:
    """检测 lark-cli 应用凭证是否已配置（bot 身份）"""
    cfg = load_lark_cli_raw_config()
    if cfg.get("appId") and cfg.get("appSecret"):
        return True
    try:
        result = subprocess_run("lark-cli config show", timeout=10)
        if result.returncode != 0:
            return False
        output = result.stdout.strip()
        return "appId" in output and "appSecret" in output
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def load_lark_cli_raw_config() -> Dict[str, Any]:
    """读取 lark-cli 本地配置，返回原始配置字典。"""
    home = resolve_lark_home() or os.path.expanduser("~")
    config_path = os.path.join(home, ".lark-cli", "config.json")
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # 兼容旧结构: {appId, appSecret, ...}
        if "appId" in data and "appSecret" in data:
            return data
        # 兼容新结构: {apps: [{appId, appSecret, ...}, ...]}
        apps = data.get("apps", [])
        if isinstance(apps, list):
            for app in apps:
                if isinstance(app, dict) and app.get("appId") and app.get("appSecret"):
                    return app
        return {}
    except Exception:
        return {}


def verify_tenant_access_token(app_id: str, app_secret: str) -> None:
    """按飞书标准流程获取 tenant_access_token，验证凭证可用。"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise LarkCliAuthError(f"请求 tenant_access_token 失败: {e}")

    try:
        payload = json.loads(text)
    except Exception:
        raise LarkCliAuthError(f"tenant_access_token 响应不可解析: {text[:300]}")

    code = payload.get("code", -1)
    token = payload.get("tenant_access_token", "")
    if code != 0 or not token:
        msg = payload.get("msg", "")
        raise LarkCliAuthError(
            f"tenant_access_token 获取失败: code={code}, msg={msg}"
        )


# ----------------------------------------------------------------------------
# lark-cli runners
# ----------------------------------------------------------------------------
def run_lark_cli(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """执行 lark-cli 命令，以 bot（应用）身份运行。

    使用 bot 身份只需 App ID + App Secret，不需要申请用户身份权限。
    前提：需要将目标文档/表格共享给该应用（添加应用为协作者）。

    参数:
        args: lark-cli 子命令及参数列表（不含 "lark-cli" 前缀）
        timeout: 超时秒数

    返回:
        subprocess.CompletedProcess

    异常:
        LarkCliNotFoundError: lark-cli 未安装
        LarkCliError: 命令执行失败
    """
    cmd_parts = ["lark-cli"] + args + ["--as", "bot"]
    cmd = subprocess.list2cmdline(cmd_parts)
    try:
        result = subprocess_run(cmd, timeout=timeout)
    except FileNotFoundError:
        raise LarkCliNotFoundError(
            "lark-cli 未安装。请先执行以下命令安装:\n"
            "  npm install -g @larksuite/cli\n"
            "安装后运行:\n"
            "  lark-cli config init  # 配置应用凭证（App ID / App Secret）"
        )
    except subprocess.TimeoutExpired:
        raise LarkCliError(f"lark-cli 命令超时（{timeout}s）: {cmd}")

    if result.returncode != 0:
        output = combine_cli_output(result)
        try:
            raise_lark_cli_error_for_output(output)
        except LarkCliAuthError:
            raise
        except LarkCliError as e:
            raise LarkCliError(
                f"lark-cli 命令失败 (exit={result.returncode}):\n"
                f"  命令: {cmd}\n"
                f"  错误: {e}"
            )
    return result


def which_lark_cli() -> Optional[str]:
    """定位 lark-cli 可执行文件，优先 WSL/Linux 原生安装。

    WSL 会继承 Windows 的 PATH，``shutil.which("lark-cli")`` 因此可能命中
    ``/mnt/c/.../npm/lark-cli`` 这个 Windows npm shim——它指向 Windows
    ``node_modules`` 下的二进制，在 Linux 下无法执行（``run_api`` 通道会直接
    报 ``binary not found at /mnt/c/...``）。这里复用 ``lark_subprocess_env``
    已把 ``~/.npm-global/bin`` 等前置过的 PATH，并在 posix 上先排除 ``/mnt/``
    入口，确保拿到原生安装；仅当没有原生安装时才回退到完整 PATH。
    """
    import shutil

    env = lark_subprocess_env()
    search_path = env.get("PATH") or os.environ.get("PATH") or ""
    if os.name != "nt" and search_path:
        non_mnt = os.pathsep.join(
            p for p in search_path.split(os.pathsep) if p and not p.startswith("/mnt/")
        )
        native = shutil.which("lark-cli", path=non_mnt or None)
        if native:
            return native
    return shutil.which("lark-cli", path=search_path or None)


def resolve_lark_cli_node_args() -> List[str]:
    """解析 lark-cli 的实际 Node.js 入口，返回 [node_path, script_path]。

    查找顺序：
    1. 打包后的内嵌 runtime（安装目录/runtime/node + node_modules）
    2. 系统 PATH 上的 lark-cli（开发环境 / 全局安装）

    Windows 上 lark-cli.CMD 是一层 cmd.exe 包装，直接调用时 cmd.exe
    会解析参数中的 & " 等字符。通过定位底层 JS 文件，可以用 node 直接
    执行，配合 shell=False 完全绕过 cmd.exe 的参数解析问题。

    WSL 下还需避开继承自 Windows PATH 的 ``/mnt/c`` npm shim（见
    ``which_lark_cli``），否则 ``run_api`` 会用一个跑不起来的 Windows 二进制。
    """
    import shutil

    # ── 1) 优先使用安装目录下的内嵌 runtime ──
    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).parent
        embedded_node = app_dir / "runtime" / "node" / "node.exe"
        embedded_entry = (
            app_dir / "runtime" / "node_modules"
            / "@larksuite" / "cli" / "scripts" / "run.js"
        )
        if embedded_node.exists() and embedded_entry.exists():
            return [str(embedded_node), str(embedded_entry)]

    # ── 2) 回退到系统 PATH（优先 WSL/Linux 原生安装）──
    lark_cmd = which_lark_cli()
    if not lark_cmd:
        hint = ""
        if getattr(sys, "frozen", False):
            hint = (
                "\n提示: 安装目录下的 runtime 也未找到。"
                "\n请手动安装 Node.js + lark-cli。"
            )
        raise LarkCliNotFoundError(
            "lark-cli 未安装。请先执行以下命令安装:\n"
            "  npm install -g @larksuite/cli" + hint
        )

    if os.name == "nt" and lark_cmd.lower().endswith(".cmd"):
        lark_dir = os.path.dirname(lark_cmd)
        js_entry = os.path.join(
            lark_dir, "node_modules", "@larksuite", "cli", "scripts", "run.js"
        )
        if os.path.isfile(js_entry):
            node_path = shutil.which("node") or "node"
            return [node_path, js_entry]

    return [lark_cmd]


def run_lark_node(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    """统一的 node + lark-cli 子进程封装，自动注入隔离 HOME 与 UTF-8 编码。"""
    return subprocess.run(
        cmd, shell=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        env=lark_subprocess_env(),
    )


def run_subprocess_with_kill(cmd: list, timeout: int = 120) -> subprocess.CompletedProcess:
    """执行子进程，超时后强制终止整个进程树（Windows 兼容）。

    subprocess.run(timeout=N) 在 Windows 上对 node.js 子进程可能无法正确终止，
    导致进程永久挂起。此函数使用 Popen + taskkill /T 确保完全终止。
    """
    proc = subprocess.Popen(
        cmd, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=lark_subprocess_env(),
    )
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=10,
            )
        else:
            proc.kill()
        stdout_bytes, stderr_bytes = proc.communicate(timeout=5)
        raise
    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def run_api(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    file: str = "",
    timeout: int = 120,
) -> Dict[str, Any]:
    """以 bot 身份调用任意飞书 OpenAPI 端点（``lark-cli api``）。

    通过 node 直接执行 lark-cli JS 入口（shell=False），避免 Windows cmd.exe
    误解析 ``&`` ``"`` 等字符；params / data 以 JSON 字符串透传。

    参数:
        method: HTTP 方法，如 GET / POST / PUT / PATCH / DELETE
        path: OpenAPI 路径，如 ``/im/v1/messages``（不含域名前缀）
        params: query 参数字典
        data: 请求体字典
        file: 可选的本地文件路径（上传场景），透传为 ``--file file=<path>``
        timeout: 超时秒数

    返回:
        解析后的 JSON 响应字典；非法 JSON 时回退为 ``{"raw_output": <text>}``。

    异常:
        LarkCliNotFoundError / LarkCliAuthError / LarkCliError
    """
    node_args = resolve_lark_cli_node_args()
    cmd = node_args + ["api", method.upper(), path, "--as", "bot"]
    if params:
        cmd += ["--params", json.dumps(params, ensure_ascii=False)]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    if file:
        cmd += ["--file", f"file={file}"]

    try:
        result = run_lark_node(cmd, timeout=timeout)
    except FileNotFoundError:
        raise LarkCliNotFoundError(
            "lark-cli 未安装。请先执行以下命令安装:\n"
            "  npm install -g @larksuite/cli"
        )
    except subprocess.TimeoutExpired:
        raise LarkCliError(f"lark-cli api {method} {path} 超时（{timeout}s）")

    if result.returncode != 0:
        raise_lark_cli_error_for_output(combine_cli_output(result))

    text = (result.stdout or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        if is_auth_error_text(text.lower()):
            raise_lark_cli_error_for_output(text)
        return {"raw_output": text}
    raise_if_api_error(payload, f"api {method} {path}")
    return payload


# ----------------------------------------------------------------------------
# Readiness / configuration
# ----------------------------------------------------------------------------
def ensure_lark_cli_ready() -> None:
    """确保 lark-cli 可用且凭证就绪。

    如果存在隔离 HOME，``lark_subprocess_env()`` 会指向 runtime/lark_home；
    否则使用系统默认 HOME 下的 lark-cli 配置。

    本函数只做轻量校验：
    1. lark-cli 二进制（或安装目录下的 runtime/node + node_modules）能找到
    2. 隔离 HOME 下存在 .lark-cli/config.json
    """
    home = resolve_lark_home()
    if home:
        cfg_path = os.path.join(home, ".lark-cli", "config.json")
        if not os.path.isfile(cfg_path):
            raise LarkCliAuthError(
                f"应用内嵌飞书凭证缺失: {cfg_path}\n"
                "请补齐 runtime/lark_home 下的 lark-cli 配置。"
            )
        cfg = load_lark_cli_raw_config()
        app_id = str(cfg.get("appId", "")).strip()
        app_secret_raw = cfg.get("appSecret", "")
        app_secret = str(app_secret_raw).strip() if isinstance(app_secret_raw, str) else ""
        if not app_id:
            raise LarkCliAuthError(
                "应用内嵌飞书凭证缺少 appId，请重新打包。"
            )
        # appSecret 可能是明文字符串，也可能是 keychain 引用（dict）。
        # 明文情况下做一次预检以提早暴露问题；keychain 情况只能交给 lark-cli 自己处理。
        if app_secret:
            try:
                verify_tenant_access_token(app_id, app_secret)
            except LarkCliAuthError as e:
                print(f"[警告] tenant_access_token 预检查未通过，将继续调用 lark-cli: {e}")
        return

    # 未隔离 HOME（开发期 + 用户没有放凭证文件）：回退到旧检测逻辑
    if not check_lark_cli_installed():
        raise LarkCliNotFoundError(
            "lark-cli 未安装。请手动执行:\n"
            "  npm install -g @larksuite/cli"
        )
    if not check_lark_cli_auth():
        raise LarkCliAuthError(
            "lark-cli 凭证未配置。\n"
            "请执行: lark-cli config init --new\n"
            "WSL 部署可在 deploy/wsl/secrets/feishu_app.json 填入凭证后重新部署。"
        )


def initiate_config_init() -> str:
    """发起应用凭证配置流程。

    调用 lark-cli config init，交互式引导用户输入 App ID 和 App Secret。
    配置完成后即可以 bot 身份操作飞书资源。

    返回:
        lark-cli 的输出文本（配置结果）
    """
    if not check_lark_cli_installed():
        raise LarkCliNotFoundError(
            "lark-cli 未安装，无法配置。请先安装:\n"
            "  npm install -g @larksuite/cli"
        )

    try:
        result = subprocess_run("lark-cli config init --new", timeout=300)
        output = result.stdout.strip()
        if result.returncode == 0:
            return output or "应用凭证配置成功"
        return f"配置流程结束（exit={result.returncode}）:\n{output}\n{result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return "配置流程超时（300s），请手动运行: lark-cli config init"
    except FileNotFoundError:
        raise LarkCliNotFoundError(
            "lark-cli 未安装，无法配置。请先安装:\n"
            "  npm install -g @larksuite/cli"
        )


__all__ = [
    "LarkCliError",
    "LarkCliNotFoundError",
    "LarkCliAuthError",
    "combine_cli_output",
    "is_auth_error_text",
    "extract_troubleshooter_url",
    "raise_lark_cli_error_for_output",
    "raise_if_api_error",
    "resolve_lark_home",
    "lark_subprocess_env",
    "subprocess_run",
    "check_lark_cli_installed",
    "check_lark_cli_auth",
    "load_lark_cli_raw_config",
    "verify_tenant_access_token",
    "run_lark_cli",
    "which_lark_cli",
    "resolve_lark_cli_node_args",
    "run_lark_node",
    "run_subprocess_with_kill",
    "run_api",
    "ensure_lark_cli_ready",
    "initiate_config_init",
]
