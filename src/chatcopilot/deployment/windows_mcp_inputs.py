"""Audit original MCP archives and their locked declarations; never build, import, install or accept them."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import tomllib


from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.dependency_closure import windows_mcp_dependency_input_inventory
from chatcopilot.core.python_plan import PythonPlanLimits,resolve_python_inputs,windows_python_plan
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.core.tar_inventory import TarInventoryLimits,read_git_source_members
from chatcopilot.core.wheel_plan import PinnedWheel
from chatcopilot.core.mcp_inputs import locked_dependency_inputs
from chatcopilot.core.mcp_project import RECIPE as PROJECT_RECIPE, windows_mcp_project_plan
from chatcopilot.core.python_environment import canonical_digest
from chatcopilot.deployment.windows_python_inputs import LOCK as PYTHON_LOCK,MCP as LOCK,METADATA

MEMBER="windows-mcp-inputs.json"
SOURCE="mcp-inputs/windows-mcp-source.tar.gz"
PYTHON="mcp-inputs/windows-mcp-python.tar.gz"
WHEELS="mcp-wheels"
SCHEMA="agentstrata-windows-mcp-inputs/v2"
MAPPED_SCHEMA="agentstrata-windows-mcp-inputs/v3"
LAYOUT="windows-mcp-installation-plan.json"
_ARCHIVE_LIMITS=TarInventoryLimits(max_archive_bytes=128*1024*1024,max_stream_bytes=512*1024*1024,
    max_total_bytes=256*1024*1024,max_member_bytes=128*1024*1024,max_members=20000,max_tree_entries=40000)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()+b"\n"


def _json(raw):
    def unique(pairs):
        value={}
        for name,item in pairs:
            if name in value:
                raise ValueError("Repeated MCP source key")
            value[name]=item
        return value
    try:
        value=json.loads(raw,object_pairs_hook=unique)
    except (ValueError,UnicodeError,RecursionError):
        raise ValueError("Malformed MCP source declaration") from None
    if type(value) is not dict:
        raise ValueError("MCP source declaration requires an object")
    return value


def _read(path,root,limit,expected=None):
    observed=trusted_source_sha256(path,root=root,max_bytes=limit)
    with path.open("rb") as stream:
        raw=stream.read(limit+1)
    if len(raw)>limit or _sha(raw)!=observed or expected is not None and observed!=expected:
        raise ValueError("MCP bytes differ from frozen source or budget")
    return raw


def _frozen(payload,manifest,name):
    if name not in manifest:
        raise ValueError("MCP input lacks its frozen source declaration")
    return _read(payload/"source"/name,payload/"source",64*1024,manifest[name]["sha256"])


def _policy(payload,manifest):
    if LOCK not in manifest:
        return None
    value=_json(_frozen(payload,manifest,LOCK))
    if "schema" not in value:
        return None  # Legacy source remains blocked elsewhere, never promoted here.
    if type(value["schema"]) is not str:
        raise ValueError("Unsupported MCP archive source policy")
    mapped=value["schema"]==MAPPED_SCHEMA
    if (set(value)!={"schema","repository","revision","archive_url","archive_sha256","package_version","python","uv","source_metadata"} | ({"installation_recipe"} if mapped else set())
            or value["schema"] not in {SCHEMA,MAPPED_SCHEMA}
            or mapped and value["installation_recipe"]!=PROJECT_RECIPE
            or value["repository"]!="https://github.com/CursorTouch/Windows-MCP"
            or type(value["revision"]) is not str or re.fullmatch(r"[a-f0-9]{40}",value["revision"]) is None
            or value["archive_url"]!="https://codeload.github.com/CursorTouch/Windows-MCP/tar.gz/"+value["revision"]
            or type(value["source_metadata"]) is not dict or set(value["source_metadata"])!={"pyproject.toml","uv.lock"}):
        raise ValueError("Unsupported MCP archive source policy")
    uv=value["uv"]
    if (type(uv) is not dict or set(uv)!={"version","url","sha256","binary_sha256"} or type(uv["version"]) is not str
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+",uv["version"]) is None
            or uv["url"]!="https://github.com/astral-sh/uv/releases/download/"+uv["version"]+"/uv-x86_64-pc-windows-msvc.zip"):
        raise ValueError("Unsupported MCP bootstrap tool declaration")
    for pin in (value["archive_sha256"],*value["source_metadata"].values(),uv["sha256"],uv["binary_sha256"]):
        if type(pin) is not str or re.fullmatch(r"[a-f0-9]{64}",pin) is None:
            raise ValueError("MCP source requires exact frozen SHA256 pins")
    return value



def reserved_member(name):
    return name in {MEMBER,LAYOUT} or name.startswith(("mcp-inputs/",WHEELS+"/"))


def _required(payload,manifest,bot_relative):
    if (type(bot_relative) is not str or bot_relative not in manifest or not bot_relative.startswith("bots/")
            or not bot_relative.endswith("/bot.yaml")):
        raise ValueError("MCP input audit requires this package's explicit application")
    policy=_policy(payload,manifest)
    return policy if bot_relative.removesuffix("bot.yaml")+"components.yaml" in manifest else None


def build_mcp_inputs(payload: Path,manifest: dict,source: bytes,python_archive: bytes,
                     wheel_inputs: tuple[PinnedWheel,...] | None=None) -> bytes:
    """Recompute from original immutable archives and frozen source. No report is accepted as authority."""
    if type(source) is not bytes or type(python_archive) is not bytes or max(len(source),len(python_archive))>128*1024*1024:
        raise ValueError("MCP archive bytes exceed immutable input budget")
    policy=_policy(payload,manifest)
    if policy is None:
        raise ValueError("MCP audit requires its original frozen archive policy")
    pylock=_frozen(payload,manifest,PYTHON_LOCK)
    metadata=_read(payload/METADATA,payload,8*1024*1024)
    resolved=resolve_python_inputs(pylock,metadata,expected_inputs_sha256=manifest[PYTHON_LOCK]["sha256"])
    pin=next((item for item in resolved.archives if item.lane=="windows_mcp_python"),None)
    if pin is None or pin.version!=policy["python"]:
        raise ValueError("MCP executor has no matching separately pinned Python archive")
    prefix="Windows-MCP-"+policy["revision"]+"/"
    inventory,files=read_git_source_members(source,expected_sha256=policy["archive_sha256"],expected_revision=policy["revision"],
        members=tuple(prefix+name for name in ("pyproject.toml","uv.lock")),limits=_ARCHIVE_LIMITS)
    if (any(not file.path.startswith(prefix) for file in inventory.files)
            or any(path!=prefix.rstrip("/") and not path.startswith(prefix) for path in inventory.directories)):
        raise ValueError("MCP source archive escaped its exact revision root")
    selected={name:files[prefix+name] for name in ("pyproject.toml","uv.lock")}
    if any(_sha(data)!=policy["source_metadata"][name] for name,data in selected.items()):
        raise ValueError("MCP original project/lock metadata differs from its reviewed source pins")
    try:
        project=tomllib.loads(selected["pyproject.toml"].decode("utf-8"))
        lock=tomllib.loads(selected["uv.lock"].decode("utf-8"))
        if project["project"]["name"]!="windows-mcp" or project["project"]["version"]!=policy["package_version"]:
            raise ValueError("MCP source package identity differs from the frozen declaration")
        graph=locked_dependency_inputs(project,lock,pin.version)
    except (UnicodeError,KeyError,TypeError,RecursionError):
        raise ValueError("MCP original project and lock metadata are unsupported") from None
    python=windows_python_plan(pylock,metadata,python_archive,expected_inputs_sha256=manifest[PYTHON_LOCK]["sha256"],
        lane="windows_mcp_python",limits=PythonPlanLimits(archive=_ARCHIVE_LIMITS))
    report={"schema":"agentstrata-windows-mcp-input-audit/v1","state":"audited-archives",
        "source_digest":manifest_digest(manifest),"source_lock_sha256":manifest[LOCK]["sha256"],
        "python_inputs_sha256":manifest[PYTHON_LOCK]["sha256"],"supplier_metadata_sha256":resolved.metadata_sha256,
        "source_archive":{"sha256":inventory.archive_sha256,"bytes":len(source),"tar_sha256":inventory.tar_sha256,
            "revision":policy["revision"],"root":prefix,"files":len(inventory.files),"directories":len(inventory.directories),
            "inventory_sha256":_sha(_canonical(asdict(inventory))),"source_metadata":policy["source_metadata"]},
        "python_archive":{"sha256":pin.sha256,"bytes":len(python_archive),"version":pin.version,"lane":pin.lane,
            "files":len(python.files),"directories":len(python.directories),"plan_sha256":_sha(_canonical(asdict(python)))},
        "dependency_inputs":graph,"build_inputs":{"project":project.get("build-system"),
            "extra_build_dependencies":project.get("tool",{}).get("uv",{}).get("extra-build-dependencies",{})},
        "wheel_archives_verified":False,"project_installation_verified":False,"prefix_loading_abi_verified":False,
        "installation_accepted":False,"target_programs_executed":False}
    if wheel_inputs is not None:
        selected_wheels={row["sha256"]:row for row in graph["selected"]}
        if (graph["blockers"] or type(wheel_inputs) is not tuple or len(wheel_inputs)!=len(selected_wheels)
                or {wheel.sha256 for wheel in wheel_inputs}!=selected_wheels.keys()
                or any(wheel.name!=selected_wheels[wheel.sha256]["name"] or wheel.version!=selected_wheels[wheel.sha256]["version"]
                    or type(wheel.data) is not bytes or len(wheel.data)!=selected_wheels[wheel.sha256]["bytes"] for wheel in wheel_inputs)):
            raise ValueError("MCP original wheel set differs from its frozen lock selection")
        verified=windows_mcp_dependency_input_inventory(wheel_inputs,python_version=pin.version,requirements=tuple(graph["roots"]))
        report.update(state="verified-wheel-inputs",wheel_archives_verified=True,
            wheel_verification={"closure":asdict(verified.closure),"inventory_sha256":_sha(_canonical(asdict(verified.inventory))),
                "archives":len(verified.inventory.wheels),"source":"original-pinned-wheel-bytes",
                "format_policy":"mcp-input-legacy-root-boolean/v1"})
    raw=_canonical(report)
    if len(raw)>512*1024:
        raise ValueError("MCP audit report exceeds byte budget")
    return raw


def build_mcp_layout(payload: Path,manifest: dict,source: bytes,wheels: tuple[PinnedWheel,...],audit: bytes) -> bytes:
    policy=_policy(payload,manifest)
    if policy is None or policy["schema"]!=MAPPED_SCHEMA:
        raise ValueError("MCP layout requires its explicit source-copy policy")
    planned=windows_mcp_project_plan(source,wheels,expected_source_sha256=policy["archive_sha256"],
        expected_revision=policy["revision"],expected_project_sha256=policy["source_metadata"]["pyproject.toml"],
        expected_lock_sha256=policy["source_metadata"]["uv.lock"],python_version=policy["python"],
        recipe=policy["installation_recipe"])
    value={"schema":"agentstrata-windows-mcp-installation-layout/v1","state":"planned",
        "source_digest":manifest_digest(manifest),"source_lock_sha256":manifest[LOCK]["sha256"] ,
        "original_input_audit_sha256":_sha(audit),"recipe":planned.recipe,
        "mapping_plan_sha256":canonical_digest(asdict(planned)),
        "files":[{key:item for key,item in asdict(file).items() if key!="generated"} for file in planned.files],
        "directories":planned.directories,"source_files":planned.source_files,
        "entry_point":asdict(planned.entry_point),"wheel_entry_points":asdict(planned.wheels)["entry_points"],
        "deferred_files":asdict(planned.wheels)["deferred_files"],"build_backend_executed":False,
        "site_import":False,"runtime_prefix_verified":False,"native_abi_verified":False,"installation_accepted":False}
    raw=_canonical(value)
    if len(raw)>16*1024*1024:
        raise ValueError("MCP installation layout exceeds its byte budget")
    return raw


def prepare_native_mcp_inputs(payload: Path,manifest: dict,bot_relative: str,*,source_archive: Path | None=None,
                              python_archive: Path | None=None,wheel_cache: Path | None=None) -> dict[str,str]:
    policy=_required(payload,manifest,bot_relative)
    if policy is None:
        return {}
    pylock=_json(_frozen(payload,manifest,PYTHON_LOCK))
    python_pin=pylock["archives"]["windows_mcp_python"]["sha256"]
    if type(python_pin) is not str or re.fullmatch(r"[a-f0-9]{64}",python_pin) is None:
        raise ValueError("MCP original Python archive requires a frozen cache identity")
    cache=Path.home()/".cache/agentstrata/runtime-inputs"
    source_archive=source_archive if source_archive is not None else cache/(policy["archive_sha256"]+".tar.gz")
    python_archive=python_archive if python_archive is not None else cache/(python_pin+".tar.gz")
    source=_read(source_archive,source_archive.parent,128*1024*1024,policy["archive_sha256"])
    python=_read(python_archive,python_archive.parent,128*1024*1024,python_pin)
    report=build_mcp_inputs(payload,manifest,source,python)
    declaration=_json(report)
    if wheel_cache is None:
        candidate=Path.home()/".cache/agentstrata/mcp-wheels"/policy["archive_sha256"]
        if candidate.exists():
            wheel_cache=candidate
    wheels=[]
    if wheel_cache is not None:
        selected=declaration["dependency_inputs"]["selected"]
        if {path.name for path in wheel_cache.iterdir()}!={row["filename"] for row in selected}:
            raise ValueError("MCP original wheel cache has missing or extra members")
        for row in selected:
            data=_read(wheel_cache/row["filename"],wheel_cache,row["bytes"],row["sha256"])
            wheels.append(PinnedWheel(data,row["sha256"],row["name"],row["version"]))
        report=build_mcp_inputs(payload,manifest,source,python,tuple(wheels))
    if policy["schema"]==MAPPED_SCHEMA and not wheels:
        raise ValueError("Mapped MCP source policy requires the complete original wheel set before preparation")
    layout=build_mcp_layout(payload,manifest,source,tuple(wheels),report) if policy["schema"]==MAPPED_SCHEMA else None
    (payload/"mcp-inputs").mkdir(exist_ok=False)
    result={}
    for name,data in ((SOURCE,source),(PYTHON,python),(MEMBER,report)):
        with (payload/name).open("xb") as stream:
            stream.write(data)
        result[name]=_sha(data)
    if layout is not None:
        with (payload/LAYOUT).open("xb") as stream:
            stream.write(layout)
        result[LAYOUT]=_sha(layout)
    if wheels:
        (payload/WHEELS).mkdir(exist_ok=False)
        for row,wheel in zip(declaration["dependency_inputs"]["selected"],wheels,strict=True):
            name=WHEELS+"/"+row["filename"]
            with (payload/name).open("xb") as stream:
                stream.write(wheel.data)
            result[name]=wheel.sha256
    return result


def verify_native_mcp_inputs(payload: Path,manifest: dict,bot_relative: str,members=None) -> dict | None:
    if members is None:
        members=[name for name in (MEMBER,LAYOUT) if (payload/name).exists()]
        if (payload/"mcp-inputs").exists():
            members.extend(path.relative_to(payload).as_posix() for path in (payload/"mcp-inputs").rglob("*") if path.is_file() or path.is_symlink())
        if (payload/WHEELS).exists():
            members.extend(path.relative_to(payload).as_posix() for path in (payload/WHEELS).rglob("*") if path.is_file() or path.is_symlink())
    present={name for name in members if reserved_member(name)}
    policy=_required(payload,manifest,bot_relative)
    if policy is None:
        if present:
            raise ValueError("MCP artifacts lack this application's frozen archive policy")
        return None
    if (policy["schema"]==MAPPED_SCHEMA)!=(LAYOUT in present):
        raise ValueError("MCP source-copy policy and installation layout presence differ")
    if not {MEMBER,SOURCE,PYTHON}.issubset(present) or any(name.startswith("mcp-inputs/") and name not in {SOURCE,PYTHON} for name in present):
        raise ValueError("MCP source policy requires exactly its original source/Python archives and audit report")
    source=_read(payload/SOURCE,payload,128*1024*1024,policy["archive_sha256"])
    python=_read(payload/PYTHON,payload,128*1024*1024)
    actual=_read(payload/MEMBER,payload,512*1024)
    expected=build_mcp_inputs(payload,manifest,source,python)
    selected=_json(expected)["dependency_inputs"]["selected"]
    wheel_names={name for name in present if name.startswith(WHEELS+"/")}
    if wheel_names:
        if wheel_names!={WHEELS+"/"+row["filename"] for row in selected}:
            raise ValueError("MCP wheel artifacts differ from the complete source-bound set")
        wheels=tuple(PinnedWheel(_read(payload/WHEELS/row["filename"],payload,row["bytes"],row["sha256"]),row["sha256"],row["name"],row["version"]) for row in selected)
        expected=build_mcp_inputs(payload,manifest,source,python,wheels)
    if actual!=expected:
        raise ValueError("MCP input report differs from original archives and frozen dependency declarations")
    result=_json(expected)
    if policy["schema"]==MAPPED_SCHEMA:
        if not wheel_names:
            raise ValueError("MCP source-copy layout requires the complete original wheel set")
        layout=build_mcp_layout(payload,manifest,source,wheels,expected)
        if _read(payload/LAYOUT,payload,16*1024*1024)!=layout:
            raise ValueError("MCP installation layout differs from original pinned source and wheels")
        result["installation_layout_plan"]=_json(layout)
    return result
