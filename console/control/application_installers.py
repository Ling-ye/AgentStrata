"""Bind explicit build sources, local installer receipts and downloads to one catalog application."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory, private_lock
from chatcopilot.core.source_snapshot import git_output
from chatcopilot.harness.activation_runtime import release_source
from chatcopilot.harness.models import HarnessError
from console.control import project_evaluation, project_repair
from studio import delivery, installer
from studio.downloads import VerifiedBuildResponse


def owned_source(apps, row):
    if row["source"] == "repository":
        bot = row["instance_id"]
        if not re.fullmatch("[a-z][a-z0-9-]*", bot) or not (apps.root / "bots" / bot / "bot.yaml").is_file():
            raise ValueError("请先将此应用的公开 BotSpec 和源码纳入开发仓库")
        return apps.root, bot, None
    if row["source"] == "studio" and row["type"] != "draft":
        repo, _, bot = project_evaluation.target(apps, row)
        with apps.studio().db.connect() as db:
            assets = db.execute("SELECT assets FROM projects WHERE id=?", (row["project_id"],)).fetchone()[0]
        return repo, bot, Path(assets) if assets else None
    raise ValueError("请先确认首版方案或导入此应用可验证的公开开发源码")


def storage(apps, ident):
    return apps.state_dir / "installers" / hashlib.sha256(ident.encode()).hexdigest()


def public_id(ident, key):
    return hashlib.sha256(ident.encode()).hexdigest()[:16] + "-" + key


def commit_plan(repo, bot, kind, revision):
    prefix = f"integrations/{bot}/"
    entries = git_output(repo, "--no-replace-objects", "ls-tree", "-rz", revision, "--", prefix, f"bots/{bot}/bot.yaml")
    members = {}
    for entry in entries.split("\0"):
        if entry:
            header, path = entry.split("\t", 1)
            mode = header.split()[0]
            if mode in {"100644", "100755"}:
                members[path] = {"executable": mode == "100755"}
    descriptor = prefix + "install.json"
    spec = json.loads(git_output(repo, "--no-replace-objects", "show", f"{revision}:{descriptor}")) if descriptor in members else None
    return installer.preview(kind, bot, members, spec)


def project_receipts(apps, row):
    if row["source"] != "studio":
        return []
    studio = apps.studio()
    with studio.db.connect() as db:
        messages = list(db.execute("SELECT id,task,updated FROM messages WHERE project=? AND state='ready' "
                                   "AND build=id ORDER BY updated DESC", (row["project_id"],)))
    records = []
    for msg in messages:
        folder = studio.state_dir / "projects" / row["project_id"] / "builds" / msg["id"]
        try:
            receipt = json.loads((folder / "build-receipt.json").read_text())
            platforms = receipt.get("installers", {})
            if not platforms and receipt.get("installer_sha256"):
                platforms = {installer.WINDOWS: {"sha256": receipt["installer_sha256"]}}
            for platform, item in platforms.items():
                path = folder / installer.FILES[platform]
                if path.is_file():
                    records.append({"id": "project-" + msg["id"], "platform": platform, "state": "ready",
                                    "origin": {"kind": "candidate", "revision": receipt.get("accepted_source_digest"),
                                               "task": msg["task"]}, "updated": msg["updated"], "size": path.stat().st_size,
                                    "sha256": item["sha256"], "components": receipt.get("components", []),
                                    "docker": receipt.get("docker", False), "requirements": receipt.get("requirements", []),
                                    "package_recipe": receipt.get("recipe"), "receipt_schema": receipt.get("schema"),
                                    "images": receipt.get("images", {}), "source_directory": receipt.get("source_directory")})
        except (OSError, ValueError, KeyError):
            continue
    return records


def published_file(directory, record):
    filename = installer.FILES[record["platform"]]
    if record.get("object"):
        digest = record["sha256"]
        if not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError("安装包摘要无效")
        filename = digest + Path(filename).suffix
    return directory / filename


def catalog(apps, row):
    if row["source"] == "release":
        from console.control import released_applications
        return released_applications.catalog(apps, row)
    root = storage(apps, row["id"])
    records = project_receipts(apps, row)
    for file in root.glob("*/state.json"):
        value = json.loads(file.read_text())
        if value["state"] == "building":
            try:
                with private_lock(file.parent / "lock"):
                    value.update(state="failed", error="构建已中断，可以用同一来源重试")
            except BlockingIOError:
                pass
        previous = value.pop("published", None)
        if previous and published_file(file.parent, previous).is_file():
            records.append({**previous, "id": public_id(row["id"], file.parent.name)})
        if value["state"] == "ready" and not published_file(file.parent, value).is_file():
            value.update(state="failed", error="安装包文件缺失，请重新生成")
        records.append({**value, "id": public_id(row["id"], file.parent.name)})
    for record in records:
        if record["state"] == "ready":
            path = root / "receipts" / (record["sha256"] + ".json")
            if path.is_file():
                record["installation"] = json.loads(path.read_text())
    kind = row["type"]
    reason, commit, bot = "", "", row.get("instance_id") or ""
    plan = None
    try:
        repo, bot, _ = owned_source(apps, row)
        if Path(git_output(repo, "rev-parse", "--show-toplevel")) != repo:
            raise ValueError("当前源码尚未绑定独立 Git 仓库")
        commit = git_output(repo, "rev-parse", "--verify", "HEAD^{commit}")
        plan = commit_plan(repo, bot, kind, commit)
    except (ValueError, OSError) as exc:
        reason = str(exc) if not str(exc).startswith("Git source") else "请先准备此应用的 Git 源码基线"
    return {"platforms": list(dict.fromkeys([*(plan.platforms if plan else ()), *(r["platform"] for r in records if r.get("platform") in installer.FILES)])),
            "build_platforms": list(plan.platforms) if plan else [],
            "docker": plan.docker if plan else False, "requirements": list(plan.requirements) if plan else [], "commit": commit, "reason": reason,
            "builds": sorted(records, key=lambda r: r["updated"], reverse=True)}


def attach_release(source, destination, project, build):
    """Copy an independently accepted Studio service with its original download and receipt identities."""
    from console.control import released_applications

    if not all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{32}", v) for v in (project, build)):
        raise ValueError("请选择有效的 Studio 项目和构建")
    row = source.require("studio:" + project)
    if row["source"] != "studio":
        raise ValueError("请选择已验收的 Studio 独立服务项目")
    if any(p["id"] == project for p in destination._projects()):
        raise ValueError("目标工作台已拥有这个开发项目")
    records = [r for r in catalog(source, row)["builds"] if r["id"] == "project-" + build and r["state"] == "ready"]
    if not any(r["platform"] == installer.LINUX for r in records):
        raise ValueError("该版本没有已就绪的 Linux 服务安装包")
    origin = records[0]["origin"]
    host = project_repair.task_host(source, row, origin["task"])
    with host.lifecycle.operation(origin["task"]):
        release = release_source(host, origin["task"])
        studio = source.studio()
        receipt = json.loads((studio.directory(project) / "builds" / build / "build-receipt.json").read_text())
        if (origin != {"kind": "candidate", "task": release.task_id, "revision": release.receipt["candidate_digest"]}
                or receipt["origin"] != origin or receipt["accepted_source_digest"] != origin["revision"]
                or receipt["application"] != release.bot_id or not receipt["docker"]
                or set(receipt["installers"]) != {r["platform"] for r in records}):
            raise ValueError("构建文件与独立接受回执不匹配")
        sources = {p: studio.artifact_reference(project, build, p) for p in receipt["installers"]}
        return released_applications.save_release(destination.state_dir, row, build, receipt, release.receipt, records, sources)


def task_controller(apps, row, task, repository_host):
    if row["source"] == "studio":
        return project_repair.task_host(apps, row, task)
    host = repository_host()
    if host.summary(task)["source"].get("bot_id") != row["instance_id"]:
        raise KeyError(task)
    return host


@contextmanager
def verified_candidate(host, bot, task):
    with host.lifecycle.operation(task):
        release = release_source(host, task)
        if release.bot_id != bot or release.bot_relative != f"bots/{bot}/bot.yaml":
            raise ValueError("候选不属于当前应用")
        yield release


@contextmanager
def frozen(repo, bot, selection, work, controller, *, recipe=None):
    kind, value = selection
    if kind == "candidate":
        with verified_candidate(controller(value), bot, value) as release:
            yield (release.candidate, release.candidate_manifest, release,
                   {"kind": kind, "task": value, "revision": release.receipt["candidate_digest"]})
        return
    from chatcopilot.deployment.packaging.source import LEGAL_FILES, checkout_commit
    snapshot = work / "snapshot"
    paths = (f"integrations/{bot}/", f"bots/{bot}/", *LEGAL_FILES) if recipe in {"static", "service", "client"} else ()
    manifest = checkout_commit(repo, value, snapshot, paths=paths)
    yield snapshot, manifest, None, {"kind": kind, "revision": value}


def validate_selection(kind, value):
    if not re.fullmatch(r"[a-f0-9]{40}" if kind == "commit" else r"repair-[a-f0-9]{32}", value):
        raise ValueError("请填写完整 Git 提交 SHA 或已验收的 Harness 任务编号")



def selection_plan(apps, row, kind, value, repository_host):
    validate_selection(kind, value)
    repo, bot, _ = owned_source(apps, row)
    if kind == "commit":
        if git_output(repo, "--no-replace-objects", "cat-file", "-t", value) != "commit":
            raise ValueError("请指定本应用仓库的完整提交")
        plan = commit_plan(repo, bot, row["type"], value)
    else:
        host = task_controller(apps, row, value, repository_host)
        with verified_candidate(host, bot, value) as release:
            plan = installer.source_plan(row["type"], bot, release.candidate, release.candidate_manifest)
    return {"recipe": plan.recipe, "platforms": list(plan.platforms)}


def register(apps, row, platform, kind, value, repository_host):
    validate_selection(kind, value)
    repo, bot, assets = owned_source(apps, row)
    app_kind = row["type"]
    if platform not in installer.FILES:
        raise ValueError("请先接入这个应用的平台构建输入")
    selected_recipe = None
    if kind == "candidate":
        for record in project_receipts(apps, row):
            if record["platform"] == platform and record["origin"].get("task") == value:
                artifact, expected = apps.studio().artifact_reference(row["project_id"], record["id"][8:], platform)
                if delivery.digest(artifact) == expected:
                    return record["id"], None
        task_controller(apps, row, value, repository_host)
    elif Path(git_output(repo, "rev-parse", "--show-toplevel")) != repo or git_output(repo, "cat-file", "-t", value) != "commit":
        raise ValueError("此应用的开发仓库中没有指定提交")
    else:
        plan = commit_plan(repo, bot, app_kind, value)
        if platform not in plan.platforms:
            raise ValueError("所选版本不支持这个安装平台")
        selected_recipe = plan.recipe
    key = hashlib.sha256(f"{platform}/{kind}/{value}".encode()).hexdigest()
    private_directory(apps.state_dir)
    private_directory(apps.state_dir / "installers")
    directory = private_directory(private_directory(storage(apps, row["id"])) / key)
    path = directory / "state.json"
    try:
        with private_lock(directory / "lock"):
            previous = json.loads(path.read_text()) if path.is_file() else {}
            ready = previous if previous.get("state") == "ready" else previous.get("published")
            if ready:
                artifact = published_file(directory, ready)
                if not artifact.is_file() or delivery.digest(artifact) != ready["sha256"]:
                    ready = None
                elif (ready.get("package_recipe") and (selected_recipe is None or ready["package_recipe"] == selected_recipe)
                      and ready.get("recipe") == installer.recipe_digest(ready["package_recipe"], platform)):
                    return public_id(row["id"], key), None
            row_state = {"id": key, "platform": platform, "selection": value, "state": "queued",
                         "origin": {"kind": kind, "revision": value}, "updated": time.time()}
            if ready:
                row_state["published"] = ready
            write_private(path, json.dumps(row_state))
    except BlockingIOError:
        return public_id(row["id"], key), None

    def run():
        try:
            with private_lock(directory / "lock"):
                if json.loads(path.read_text()).get("state") == "ready":
                    return
                def save(**changes):
                    row_state.update(**changes, updated=time.time())
                    write_private(path, json.dumps(row_state))
                save(state="building")
                try:
                    with tempfile.TemporaryDirectory(dir=directory) as temp:
                        work = Path(temp)
                        def controller(task):
                            return task_controller(apps, row, task, repository_host)
                        with frozen(repo, bot, (kind, value), work, controller, recipe=selected_recipe) as (source, manifest, release, origin):
                            from chatcopilot.core.application_configuration import load_application_spec
                            if load_application_spec(source / "bots" / bot / "bot.yaml").raw.get("id") != bot:
                                raise ValueError("所选源码不包含本应用的声明")
                            result = installer.build(source, work / "output", bot=bot, name=row["name"], kind=app_kind,
                                                     manifest=manifest, origin=origin, targets=[platform], release=release, assets=assets)
                            info = result["installers"][platform]
                        file = published_file(directory, {"object": True, "platform": platform, **info})
                        (work / "output" / installer.FILES[platform]).replace(file)
                        previous = row_state.pop("published", None)
                        save(state="ready", origin=origin, object=True, docker=result["docker"], recipe=result["recipe_fingerprints"][platform],
                             package_recipe=result["recipe"], receipt_schema=result["schema"], images=result.get("images", {}),
                             source_directory=result.get("source_directory"),
                             requirements=result["requirements"], components=result["components"], **info)
                        if previous:
                            old_file = published_file(directory, previous)
                            if old_file != file:
                                old_file.unlink(missing_ok=True)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, HarnessError, KeyError) as exc:
                    message = str(exc) if isinstance(exc, ValueError) and not str(exc).startswith("Git source") else "生成失败，请核对所选源码、构建工具和验收记录后重试"
                    save(state="failed", error=message)
        except BlockingIOError:
            pass
    return public_id(row["id"], key), run


def import_installation(apps, row, key, platform, receipt):
    ready = next((r for r in catalog(apps, row)["builds"] if r["id"] == key and
                  r["platform"] == platform and r["state"] == "ready" and r["sha256"] == receipt.sha256), None)
    if not ready:
        raise KeyError(key)
    _, bot, _ = owned_source(apps, row)
    if (receipt.application != bot or receipt.platform != platform or receipt.services_ready != bool(ready.get("docker"))
            or receipt.completed_at > time.time() + 300):
        raise ValueError("接收方记录与这个应用、平台或安装包不匹配")
    # Receiver-reported installation never changes build, independent acceptance or activation state.
    root = private_directory(private_directory(apps.state_dir / "installers") / hashlib.sha256(row["id"].encode()).hexdigest())
    folder = private_directory(root / "receipts")
    path = folder / (receipt.sha256 + ".json")
    with private_lock(folder / "lock", timeout=5):
        if not path.exists() or json.loads(path.read_text())["completed_at"] <= receipt.completed_at:
            write_private(path, json.dumps({"completed_at": receipt.completed_at, "services_ready": receipt.services_ready}))
    return {"recorded": True}


def download(apps, row, key, platform):
    if row["source"] == "release":
        from console.control import released_applications
        artifact, digest = released_applications.artifact(apps, row, key, platform)
    elif re.fullmatch(r"project-[a-f0-9]{32}", key) and row["source"] == "studio":
        artifact, digest = apps.studio().artifact_reference(row["project_id"], key[8:], platform)
        if artifact.suffix not in {".exe", ".run"}:
            raise KeyError(key)
    elif re.fullmatch(r"[a-f0-9]{16}-[a-f0-9]{64}", key) and key == public_id(row["id"], key[17:]):
        directory = storage(apps, row["id"]) / key[17:]
        if not (directory / "state.json").is_file():
            raise KeyError(key)
        state = json.loads((directory / "state.json").read_text())
        ready = state if state["state"] == "ready" else state.get("published")
        if not ready or ready["platform"] != platform:
            raise KeyError(key)
        artifact, digest = published_file(directory, ready), ready["sha256"]
    else:
        raise KeyError(key)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", row["name"]).strip(" .") or "AgentStrata"
    return VerifiedBuildResponse(artifact, digest, filename=name + "-Setup" + artifact.suffix)
