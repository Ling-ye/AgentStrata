"""Build a declared application from one Git commit without a Studio instance."""
import errno
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from .builder import build
from .plan import application_prefix, read_plan
from .source import LEGAL_FILES, checkout_commit


def package_commit(source: Path, revision: str, application: str, name: str, output: Path, platforms=None, *, application_dir=None):
    if sys.platform not in {"linux", "win32"}:
        raise ValueError("当前构建端缺少不覆盖目标的原子发布，请使用 Linux/WSL")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("安装包输出目录已存在")
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="agentstrata-package-") as checkout, TemporaryDirectory(dir=output.parent) as stage:
        snapshot = Path(checkout) / "source"
        prefix = application_prefix(application, application_dir)
        bot_spec = f"bots/{application}/bot.yaml"
        paths = (prefix, *LEGAL_FILES) if prefix else ()
        if application_dir is None:
            paths += (bot_spec,)
        manifest = checkout_commit(source.resolve(), revision, snapshot, paths=paths)
        default_recipe = None
        if application_dir is None and bot_spec in manifest:
            from chatcopilot.core.application_configuration import read_configuration_yaml
            declaration = read_configuration_yaml(snapshot / bot_spec)
            if (declaration.get("deploy") or {}).get("target") == "windows":
                default_recipe = "native"
            elif declaration.get("channels") and prefix + "install.json" not in manifest:
                raise ValueError("此 Gateway 需要宿主配方。请从 Studio 构建渠道应用，或为独立服务提供 install.json")
        plan = read_plan(snapshot, manifest, application, application_dir=application_dir, default_recipe=default_recipe)
        if plan.recipe == "native":
            raise ValueError("此应用使用原生 Windows 宿主。请从 Studio 构建原生安装包")
        pending = Path(stage) / "release"
        receipt = build(snapshot, pending, bot=application, name=name, manifest=manifest,
                        origin={"kind": "commit", "revision": revision}, plan=plan, targets=platforms)
        _publish(pending, output)
    return receipt


def _publish(pending: Path, output: Path) -> None:
    try:
        if sys.platform == "linux":
            # A plain POSIX rename replaces an empty destination directory.
            from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno

            at_fdcwd, rename_noreplace = -100, 1
            libc = CDLL(None, use_errno=True)
            try:
                rename = libc.renameat2
            except AttributeError:
                raise OSError(errno.ENOSYS, "构建端缺少不覆盖目标的原子目录发布") from None
            rename.argtypes = (c_int, c_char_p, c_int, c_char_p, c_uint)
            rename.restype = c_int
            if rename(at_fdcwd, os.fsencode(pending), at_fdcwd, os.fsencode(output), rename_noreplace):
                code = get_errno()
                raise OSError(code, os.strerror(code), output)
        else:
            # Windows rename already fails when the destination exists.
            pending.rename(output)
    except FileExistsError:
        raise ValueError("安装包输出目录已存在") from None
