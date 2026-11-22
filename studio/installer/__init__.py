"""Studio adapters around the application-category-neutral packaging engine."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import Mapping

from chatcopilot.deployment.packaging import FILES, LINUX, WINDOWS, PackagePlan, read_plan, resolve_plan
from chatcopilot.deployment.packaging import builder as shared_builder
from chatcopilot.deployment.packaging import containers
from studio import delivery
from .gateway_recipe import context_provider
from .native_recipe import build as build_native
from .nsis import compile as compile_nsis
from .recipes import BOTS, default_recipe, platforms as default_platforms
from .recipes import requirements as default_requirements


RECIPES = Path(__file__).parent
NATIVE_SETUP = "首次打开时按提示准备本地运行环境，可能需要联网下载依赖，并绑定自己的模型账号。"


def recipe_digest(recipe, platform):
    """Cache identity for the selected recipe and its target envelope."""
    root = RECIPES.parents[1]
    shared = root / "src/chatcopilot/deployment/packaging"
    templates = shared / "templates"
    sources = [shared / "plan.py", shared / "source.py", RECIPES / "__init__.py", RECIPES / "recipes.py"]
    if recipe == "native":
        desktop = root / "integrations/desktop_app"
        sources.extend(desktop / name for name in (
            "build.ps1", "package.cjs", "cross-native.nsi", "nsis.cjs", "main.cjs", "preload.cjs",
            "setup.html", "setup.js", "desktop.css",
        ))
        live = root / "integrations/live2d_desktop"
        sources.extend((templates / "install-receipt.ps1", root / "studio/delivery.py", RECIPES / "native_recipe.py",
                        live / "scripts/prepare-runtime.py", live / "scripts/bootstrap-windows.ps1",
                        live / "package.json", live / "package-lock.json", live / "src/backend.ts", live / "src/contracts.ts"))
        sources.extend(shared.parent.glob("windows_*.py"))
    else:
        sources.extend((shared / "builder.py", shared / "formats.py"))
        if platform == WINDOWS:
            sources.extend(templates / name for name in ("windows.nsi", "install.ps1", "install-receipt.ps1"))
            sources.extend((RECIPES / "nsis.py", root / "integrations/desktop_app/nsis.cjs",
                            root / "integrations/live2d_desktop/package-lock.json"))
            if recipe in {"service", "gateway"}:
                sources.append(templates / "open-client.ps1")
        else:
            sources.append(templates / "install.sh")
        if recipe in {"service", "gateway"}:
            sources.append(shared / "containers.py")
        if recipe == "gateway":
            sources.extend((RECIPES / "gateway_recipe.py", RECIPES / "gateway.py", RECIPES / "gateway.Dockerfile",
                            root / "studio/delivery.py"))
    digest = hashlib.sha256()
    for path in sorted(sources):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def platforms(kind):
    return list(default_platforms(kind))


def requirements(kind, bot):
    return list(default_requirements(kind, bot))


def _defaults(kind, bot):
    return {"default_recipe": default_recipe(kind), "default_platforms": default_platforms(kind),
            "default_requirements": default_requirements(kind, bot)}


def _studio_plan(kind, plan):
    if kind == "companion" and plan.recipe != "native":
        raise ValueError("桌宠的 Windows 运行载荷须经过原生交付验证")
    return replace(plan, requirements=(NATIVE_SETUP, *plan.requirements)) if plan.recipe == "native" else plan


def preview(kind: str, bot: str, members: Mapping, spec: Mapping | None = None) -> PackagePlan:
    """Resolve the exact strategy used by build without reading mutable source files."""
    return _studio_plan(kind, resolve_plan(bot, members, spec, **_defaults(kind, bot)))


def source_plan(kind, bot, source, manifest):
    return _studio_plan(kind, read_plan(source, manifest, bot, **_defaults(kind, bot)))


def build(source, dest, *, bot, kind, name, manifest, origin, targets=None, release=None, assets=None,
          install_id=None, windows_build=None):
    """Keep the established Studio API while dispatching through shared packaging recipes."""
    plan = source_plan(kind, bot, source, manifest)
    selected = plan.select_targets(targets)
    fingerprints = {platform: recipe_digest(plan.recipe, platform) for platform in selected}
    if plan.recipe == "native":
        if release and (origin.get("kind") != "candidate" or origin["revision"] != release.receipt["candidate_digest"]):
            raise ValueError("独立接受摘要与安装包来源不匹配")
        if selected != (WINDOWS,):
            raise ValueError("原生桌面应用只支持 Windows x64")
        receipt = build_native(source, dest, bot=bot, kind=kind, name=name, origin=origin, release=release,
                               assets=assets, install_id=install_id, windows_build=windows_build)
        receipt.update(schema=shared_builder.BUILD_RECEIPT_SCHEMA, recipe=plan.recipe,
                       application=bot, origin=origin, docker=False, requirements=list(plan.requirements),
                       installers={WINDOWS: {"sha256": delivery.digest(dest / FILES[WINDOWS]),
                                             "size": (dest / FILES[WINDOWS]).stat().st_size}})
    else:
        provider = context_provider(kind) if plan.recipe == "gateway" else None
        receipt = shared_builder.build(source, dest, bot=bot, name=name, manifest=manifest, origin=origin, plan=plan,
                                       targets=selected, context_provider=provider, docker_runner=containers.docker,
                                       nsis=compile_nsis,
                                       accepted_source_digest=release.receipt["candidate_digest"] if release else None)
    if any(recipe_digest(plan.recipe, platform) != before for platform, before in fingerprints.items()):
        raise ValueError("构建期间宿主打包配方已变化，请使用同一来源重试")
    receipt["recipe_fingerprints"] = fingerprints
    (dest / "build-receipt.json").write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    return receipt


__all__ = ["BOTS", "FILES", "LINUX", "WINDOWS", "build", "platforms", "preview",
           "recipe_digest", "requirements"]
