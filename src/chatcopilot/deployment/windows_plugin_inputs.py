"""Source-bound complete plugin declarations and optional original wheel closure.

Derived build receipts, installed environments and fetched index summaries never
introduce new accepted hashes. Project/assets/loading and acceptance stay blocked.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

from chatcopilot.core.dependency_closure import (
    windows_dependency_input_inventory, windows_plugin_dependency_input_inventory, windows_python_tags, windows_requirement_fact,
)
from chatcopilot.core.file_integrity import trusted_source_sha256
from chatcopilot.core.langdetect_wheel import RECIPE as COPY_RECIPE, SOURCE_FILENAME, langdetect_wheel_copy
from chatcopilot.core.source_snapshot import manifest_digest
from chatcopilot.core.project_patch import RECIPE as PROJECT_RECIPE, project_source_mapping
from chatcopilot.core.wheel_plan import PinnedWheel, WheelPlanLimits
from chatcopilot.deployment.windows_python_inputs import verify_native_python_inputs

LOCK = "integrations/live2d_desktop/windows-plugin-inputs.lock.json"
MEMBER = "windows-plugin-inputs.json"
WHEELS = "plugin-wheels/open-llm-vtuber"
SOURCES = "plugin-sources/open-llm-vtuber"
SCHEMA = "agentstrata-windows-plugin-inputs/v1"
COPY_SCHEMA = "agentstrata-windows-plugin-inputs/v2"
COPY_STATE = "paired-source-wheel-copy"
PROJECT_SCHEMA = "agentstrata-windows-plugin-inputs/v3"
PROJECTS = "plugin-projects/open-llm-vtuber"
ARCHIVE_POLICY = "redundant-local-zip64/v1"
_RESOURCE = "integrations/open_llm_vtuber/agentstrata_vtuber/resources/"
_SOURCES = {"requirements_input":_RESOURCE+"requirements-windows.in", "requirements_lock":_RESOURCE+"requirements-windows.lock",
    "upstream":_RESOURCE+"upstream.json", "artifacts":_RESOURCE+"artifacts.json"}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",",":"), allow_nan=False).encode()+b"\n"


def _json(raw):
    def unique(pairs):
        value = {}
        for name, item in pairs:
            if name in value:
                raise ValueError("Repeated plugin input key")
            value[name] = item
        return value
    try:
        value = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Malformed plugin input document") from None
    if type(value) is not dict:
        raise ValueError("Plugin input document requires an object")
    return value


def _read(path, root, limit, pin=None):
    observed = trusted_source_sha256(path, root=root, max_bytes=limit)
    with path.open("rb") as stream:
        raw = stream.read(limit+1)
    if len(raw)>limit or _sha(raw)!=observed or pin is not None and pin!=observed:
        raise ValueError("Plugin original input differs from its source pin or budget")
    return raw


def _frozen(payload, manifest, name):
    if name not in manifest:
        raise ValueError("Plugin input lacks its frozen source declaration")
    return _read(payload/"source"/name, payload/"source", 4*1024*1024, manifest[name]["sha256"])


def _policy(payload, manifest, bot_relative):
    if (type(bot_relative) is not str or bot_relative not in manifest or not bot_relative.startswith("bots/")
            or not bot_relative.endswith("/bot.yaml")):
        raise ValueError("Plugin inputs require this package's explicit application")
    if LOCK not in manifest or bot_relative.removesuffix("bot.yaml")+"components.yaml" not in manifest:
        return None
    policy = _json(_frozen(payload,manifest,LOCK))
    copy_schema=policy.get('schema') in (COPY_SCHEMA,PROJECT_SCHEMA)
    project_schema=policy.get('schema')==PROJECT_SCHEMA
    if (set(policy)!={"schema","plugin","recipe","python_lane","direct_wheels","source_builds",*_SOURCES,
                *({'wheel_archive_policy'} if copy_schema else set()), *({'projects'} if project_schema else set())}
            or policy["schema"] not in (SCHEMA,COPY_SCHEMA,PROJECT_SCHEMA) or policy["plugin"]!="open-llm-vtuber"
            or policy["recipe"]!={SCHEMA:"windows-vtuber-original-inputs/v1",COPY_SCHEMA:"windows-vtuber-original-inputs/v2",PROJECT_SCHEMA:"windows-vtuber-original-inputs/v3"}[policy["schema"]]
            or policy["python_lane"]!="vtuber_python"
            or copy_schema and policy.get('wheel_archive_policy')!=ARCHIVE_POLICY
            or any(policy[key]!=name for key,name in _SOURCES.items())
            or type(policy["direct_wheels"]) is not dict or len(policy["direct_wheels"])>16
            or type(policy["source_builds"]) is not dict or len(policy["source_builds"])>16):
        raise ValueError("Unsupported plugin original-input source policy")
    if project_schema and (type(policy['projects']) is not dict or set(policy['projects'])!={'backend','frontend'}
            or any(type(row) is not dict or set(row)!={'recipe','allowed_cjk_paths'} or row['recipe']!=PROJECT_RECIPE
                or type(row['allowed_cjk_paths']) is not list or len(row['allowed_cjk_paths'])>16
                or any(type(name) is not str for name in row['allowed_cjk_paths']) for row in policy['projects'].values())):
        raise ValueError("Unsupported plugin project source policy")
    return policy


def _requirement(text, policy, version):
    if (type(text) is not str or not text.isascii() or not 0<len(text)<=4096
            or any(char in text for char in "\r\n\x00") or text.count("(")>16):
        raise ValueError("Plugin requirement exceeds its syntax budget")
    value = Requirement(text)
    name = canonicalize_name(value.name,validate=True)
    value.name = name
    value.extras = {canonicalize_name(extra,validate=True) for extra in value.extras}
    direct = None
    if value.url is not None:
        direct = policy["direct_wheels"].get(name)
        if (type(direct) is not dict or set(direct)!={"url","sha256"} or value.extras or value.marker
                or direct["url"]!=value.url or type(direct["sha256"]) is not str
                or re.fullmatch(r"[a-f0-9]{64}",direct["sha256"]) is None):
            raise ValueError("Plugin direct requirement lacks its independently frozen exact wheel")
        url = urlsplit(value.url)
        filename = unquote(url.path.rsplit("/",1)[-1])
        if (url.scheme!="https" or url.hostname!="download-r2.pytorch.org" or url.port not in {None,443}
                or url.username or url.password or url.query or url.fragment!="sha256="+direct["sha256"]
                or not re.fullmatch(r"[A-Za-z0-9_.!+\-]+\.whl",filename)):
            raise ValueError("Plugin direct wheel requires its explicit credential-free CPU supplier policy")
        distribution, resolved, _build, tags = parse_wheel_filename(filename)
        if distribution!=name or not {str(tag) for tag in tags}.intersection(windows_python_tags(version)):
            raise ValueError("Plugin direct wheel identity or explicit target ABI differs")
        named = name+"=="+str(resolved)
    else:
        specs = tuple(value.specifier)
        if len(specs)!=1 or specs[0].operator!="==" or "*" in specs[0].version:
            raise ValueError("Plugin declarations require exact versions")
        named = str(value)
    fact = windows_requirement_fact(named,python_version=version)
    if not fact.active:
        raise ValueError("Plugin original lock contains an inactive target requirement")
    parsed = Requirement(named)
    return {"name":name,"version":tuple(parsed.specifier)[0].version,"requirement":named,"original":text,
        "direct_sha256":direct["sha256"] if direct else None}


def plugin_dependency_declarations(requirements: bytes, locked: bytes, policy: dict, *, python_version: str) -> dict:
    """Consume the whole source input and hash lock, including the explicitly pinned CPU URL."""
    if (type(requirements) is not bytes or type(locked) is not bytes
            or max(len(requirements),len(locked))>4*1024*1024):
        raise ValueError("Plugin declaration bytes exceed their budget")
    if (type(python_version) is not str or re.fullmatch(r"3\.12\.[0-9]{1,4}",python_version) is None
            or type(policy) is not dict or type(policy.get("direct_wheels")) is not dict
            or type(policy.get("source_builds")) is not dict):
        raise ValueError("Plugin declarations require their explicit source policy and Python 3.12 target")
    windows_python_tags(python_version)
    roots=[]
    for line in requirements.decode("ascii").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            roots.append(_requirement(line,policy,python_version))
    rows=[]
    current=None
    for line in locked.decode("ascii").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        continued=line.endswith("\\")
        item=line[:-1].rstrip() if continued else line.rstrip()
        if current is None:
            if item.startswith(" "):
                raise ValueError("Plugin hash lock lacks a requirement")
            current={**_requirement(item,policy,python_version),"hashes":[]}
        else:
            if re.fullmatch(r"    --hash=sha256:[a-f0-9]{64}",item) is None:
                raise ValueError("Plugin hash lock contains an unadmitted option or hash")
            pin=item.strip().removeprefix("--hash=sha256:")
            if pin in current["hashes"]:
                raise ValueError("Plugin hash lock repeats an archive pin")
            current["hashes"].append(pin)
        if not continued:
            if not current["hashes"] or len(current["hashes"])>1024:
                raise ValueError("Plugin hash lock requires a bounded nonempty archive set")
            if current["direct_sha256"] is not None and current["hashes"]!=[current["direct_sha256"]]:
                raise ValueError("Plugin direct wheel hash differs between its source declarations")
            current["hashes"].sort()
            rows.append(current)
            current=None
    if current is not None or not 1<=len(roots)<=256 or not 1<=len(rows)<=256:
        raise ValueError("Plugin declarations are incomplete or exceed package budgets")
    by_name={row["name"]:row for row in rows}
    if len(by_name)!=len(rows) or len({row["name"] for row in roots})!=len(roots):
        raise ValueError("Plugin declarations repeat a distribution")
    for root in roots:
        if root["name"] not in by_name or any(root[key]!=by_name[root["name"]][key] for key in ("version","requirement","direct_sha256")):
            raise ValueError("Plugin original roots differ from the complete hash lock")
    used_direct={row["name"] for row in roots if row["direct_sha256"] is not None}
    if used_direct!=set(policy["direct_wheels"]):
        raise ValueError("Plugin source policy contains absent or unadmitted direct wheels")
    if "langdetect" in by_name and by_name["langdetect"]["version"]=="1.0.9" and "langdetect" not in policy["source_builds"]:
        raise ValueError("Plugin langdetect source-build admission cannot be omitted from this recipe")
    for name,build in policy["source_builds"].items():
        if name not in by_name or type(build) is not dict or build.get("version")!=by_name[name]["version"]:
            raise ValueError("Plugin source-build exception lacks an explicit unresolved source declaration")
        if build.get("state")=="unadmitted" and set(build)=={"version","state"}:
            continue
        pins=("source_sha256","original_wheel_sha256","setup_sha256")
        if (policy.get("schema") not in (COPY_SCHEMA,PROJECT_SCHEMA) or name!="langdetect" or build["version"]!="1.0.9"
                or set(build)!={"version","state","recipe",*pins} or build.get("state")!=COPY_STATE
                or build.get("recipe")!=COPY_RECIPE
                or any(type(build[key]) is not str or re.fullmatch(r"[a-f0-9]{64}",build[key]) is None for key in pins)
                or build["source_sha256"]==build["original_wheel_sha256"]
                or not {build["source_sha256"],build["original_wheel_sha256"]}.issubset(by_name[name]["hashes"])):
            raise ValueError("Plugin source-copy recipe requires its own source policy and both original hash-lock inputs")
    return {"roots":[row["requirement"] for row in roots],"locked":sorted(rows,key=lambda row:row["name"]),
        "scope":"complete-source-locked-plugin-declarations","python_version":python_version,
        "wheel_metadata_verified":False,"source_builds":policy["source_builds"]}


def reserved_member(name):
    return name==MEMBER or name.startswith(("plugin-wheels/","plugin-sources/","plugin-projects/"))


def build_plugin_inputs(payload: Path,manifest: dict,bot_relative: str, wheel_inputs: tuple[PinnedWheel,...] | None=None,
                        *, source_inputs: dict[str,bytes] | None=None, project_inputs: dict[str,bytes] | None=None) -> bytes:
    policy=_policy(payload,manifest,bot_relative)
    if policy is None:
        raise ValueError("Plugin inputs require their own frozen source policy")
    python=verify_native_python_inputs(payload,manifest)
    if python is None or "vtuber_python" not in python["archives"]:
        raise ValueError("Plugin source policy requires its separately pinned Python lane")
    version=python["archives"]["vtuber_python"]["version"]
    sources={name:_frozen(payload,manifest,name) for name in _SOURCES.values()}
    declarations=plugin_dependency_declarations(sources[policy["requirements_input"]],sources[policy["requirements_lock"]],
        policy,python_version=version)
    upstream=_json(sources[policy["upstream"]])
    artifacts=_json(sources[policy["artifacts"]])
    if set(upstream)!={"backend","frontend"} or not {"backend","frontend"}.issubset(artifacts):
        raise ValueError("Plugin source and asset declarations lack their complete projects")
    projects=_project_declarations(payload,manifest,policy,upstream,artifacts) if policy['schema']==PROJECT_SCHEMA else {}
    for declaration in projects.values():
        sources[declaration['patch_path']]=_frozen(payload,manifest,declaration['patch_path'])
    blockers=["plugin-project-archives-and-patches-unverified","plugin-assets-and-tools-unverified","plugin-prefix-loading-and-abi-unverified"]
    copies={name:build for name,build in policy["source_builds"].items() if build["state"]==COPY_STATE}
    blockers += ["plugin-source-build-not-admitted:"+name for name,build in sorted(policy["source_builds"].items()) if build["state"]=="unadmitted"]
    report={"schema":"agentstrata-windows-plugin-input-audit/v1","state":"locked-declarations",
        "source_digest":manifest_digest(manifest),"source_policy_sha256":manifest[LOCK]["sha256"],
        "source_inputs":{name:_sha(raw) for name,raw in sources.items()},"python_lane":"vtuber_python",
        "python_inputs_sha256":python["input_lock_sha256"],"supplier_metadata_sha256":python["metadata_sha256"],
        "python_archive":python["archives"]["vtuber_python"],"dependencies":declarations,
        "wheel_archives":[],"wheel_closure_verified":False,"project_installation_verified":False,
        "assets_verified":False,"prefix_loading_abi_verified":False,"installation_accepted":False,"target_programs_executed":False}
    if policy["schema"] in (COPY_SCHEMA,PROJECT_SCHEMA):
        report.update(schema="agentstrata-windows-plugin-input-audit/v2",source_archives=[],wheel_transforms=[],wheel_archive_policy=ARCHIVE_POLICY)
    if wheel_inputs is None:
        if source_inputs is not None:
            raise ValueError("Plugin paired source inputs require their complete original wheel set")
        blockers.append("plugin-complete-original-wheel-set-absent")
        blockers += ["plugin-paired-source-absent:"+name for name in sorted(copies)]
    else:
        by_name={row["name"]:row for row in declarations["locked"]}
        if (type(wheel_inputs) is not tuple or len(wheel_inputs)!=len(by_name)
                or any(type(pin) is not PinnedWheel or type(pin.data) is not bytes for pin in wheel_inputs)
                or {pin.name for pin in wheel_inputs}!=by_name.keys()
                or sum(len(pin.data) for pin in wheel_inputs)>512*1024*1024
                or any(pin.version!=by_name[pin.name]["version"] or pin.sha256 not in by_name[pin.name]["hashes"] for pin in wheel_inputs)):
            raise ValueError("Plugin raw wheels differ from the complete original source hash lock")
        if source_inputs is None:
            source_inputs={}
        if type(source_inputs) is not dict or set(source_inputs)!=set(copies) or any(type(raw) is not bytes for raw in source_inputs.values()):
            raise ValueError("Plugin paired source archives are missing or have extra inputs")
        derived=[]
        for pin in wheel_inputs:
            if pin.name not in copies:
                derived.append(pin)
                continue
            build=copies[pin.name]
            copied=langdetect_wheel_copy(source_inputs[pin.name],pin,expected_source_sha256=build["source_sha256"],
                expected_original_sha256=build["original_wheel_sha256"],expected_setup_sha256=build["setup_sha256"])
            derived.append(copied.wheel)
            report["wheel_transforms"].append(copied.audit())
            report["source_archives"].append({"name":pin.name,"version":pin.version,"member":SOURCES+"/"+SOURCE_FILENAME,
                "sha256":copied.source_archive_sha256,"bytes":len(source_inputs[pin.name])})
        inventory_builder=windows_plugin_dependency_input_inventory if policy['schema'] in (COPY_SCHEMA,PROJECT_SCHEMA) else windows_dependency_input_inventory
        checked=inventory_builder(tuple(derived),python_version=version,requirements=tuple(declarations["roots"]),wheel_limits=WheelPlanLimits())
        report.update(state="verified-wheel-inputs",wheel_closure_verified=True,
            wheel_closure=asdict(checked.closure),wheel_archives=[{"name":pin.name,"version":pin.version,"sha256":pin.sha256,"bytes":len(pin.data)} for pin in sorted(wheel_inputs,key=lambda pin:pin.name)])
    if policy['schema']==PROJECT_SCHEMA:
        report.update(schema='agentstrata-windows-plugin-input-audit/v3',project_declarations=projects,
            project_archives=[],project_source_plans=[],project_sources_verified=False)
        if project_inputs is not None:
            if (type(project_inputs) is not dict or set(project_inputs)!=set(projects)
                    or any(type(raw) is not bytes for raw in project_inputs.values())):
                raise ValueError("Plugin project inputs require both exact original archives")
            for name,declaration in projects.items():
                plan,_contents=project_source_mapping(project_inputs[name],sources[declaration['patch_path']],
                    **_project_pins(declaration))
                report['project_source_plans'].append({'name':name,**asdict(plan)})
                report['project_archives'].append({'name':name,'member':PROJECTS+'/'+name+'.tar.gz',
                    'sha256':declaration['archive_sha256'],'bytes':len(project_inputs[name])})
            report['project_sources_verified']=True
            blockers.remove('plugin-project-archives-and-patches-unverified')
            blockers.append('plugin-project-source-not-installed-or-accepted')
            blockers.append('plugin-frontend-build-and-output-unverified')
    elif project_inputs is not None:
        raise ValueError("Plugin projects lack their independently frozen project policy")
    report["blockers"]=sorted(blockers)
    raw=_canonical(report)
    if len(raw)>4*1024*1024:
        raise ValueError("Plugin input report exceeds its byte budget")
    return raw


def _cached(cache, report):
    rows={row["name"]:row for row in report["dependencies"]["locked"]}
    pins={}
    paths=tuple(sorted(cache.iterdir()))
    if len(paths)!=len(rows):
        raise ValueError("Plugin original wheel cache is missing or contains extra members")
    total=0
    for path in paths:
        if not path.name.endswith(".whl"):
            raise ValueError("Plugin original wheel cache contains an extra member")
        name,version,_build,_tags=parse_wheel_filename(path.name)
        if name not in rows or str(version)!=rows[name]["version"]:
            raise ValueError("Plugin original wheel cache contains an unpinned distribution")
        raw=_read(path,cache,128*1024*1024)
        total+=len(raw)
        if total>512*1024*1024:
            raise ValueError("Plugin original wheel cache exceeds its aggregate byte budget")
        pin=_sha(raw)
        if pin not in rows[name]["hashes"]:
            raise ValueError("Plugin original wheel digest is absent from the frozen hash lock")
        pins[path.name]=PinnedWheel(raw,pin,name,str(version))
    if len(pins)!=len(rows) or len({pin.name for pin in pins.values()})!=len(rows):
        raise ValueError("Plugin original wheel cache is missing or repeats distributions")
    return pins


def _cached_sources(cache,report):
    copies={name:build for name,build in report["dependencies"]["source_builds"].items() if build["state"]==COPY_STATE}
    if not copies:
        if cache is not None:
            raise ValueError("Plugin source cache lacks its independently frozen source-copy policy")
        return {}
    if cache is None:
        raise ValueError("Plugin paired source-copy recipe requires its original source cache")
    paths=tuple(cache.iterdir())
    if len(paths)!=1 or paths[0].name!=SOURCE_FILENAME:
        raise ValueError("Plugin paired source cache is missing or contains extra members")
    return {"langdetect":_read(paths[0],cache,2*1024*1024,copies["langdetect"]["source_sha256"])}


def prepare_native_plugin_inputs(payload: Path,manifest: dict,bot_relative: str,*,wheel_cache: Path | None=None,
                                 source_cache: Path | None=None, project_cache: Path | None=None) -> dict[str,str]:
    if _policy(payload,manifest,bot_relative) is None:
        return {}
    report=build_plugin_inputs(payload,manifest,bot_relative)
    wheels=None
    sources={}
    if source_cache is not None and wheel_cache is None:
        raise ValueError("Plugin paired source inputs require their complete original wheel set")
    if wheel_cache is not None:
        wheels=_cached(wheel_cache,_json(report))
        sources=_cached_sources(source_cache,_json(report))
        report=build_plugin_inputs(payload,manifest,bot_relative,tuple(wheels.values()),source_inputs=sources)
    projects=_cached_projects(project_cache,_json(report)) if project_cache is not None else None
    if projects is not None:
        report=build_plugin_inputs(payload,manifest,bot_relative,tuple(wheels.values()) if wheels is not None else None,
            source_inputs=sources if wheels is not None else None,project_inputs=projects)
    result={MEMBER:_sha(report)}
    if wheels is not None:
        (payload/WHEELS).mkdir(parents=True,exist_ok=False)
        for filename,pin in wheels.items():
            name=WHEELS+"/"+filename
            with (payload/name).open("xb") as stream:
                stream.write(pin.data)
            result[name]=pin.sha256
    if sources:
        (payload/SOURCES).mkdir(parents=True,exist_ok=False)
        name=SOURCES+"/"+SOURCE_FILENAME
        with (payload/name).open("xb") as stream:
            stream.write(sources["langdetect"])
        result[name]=_sha(sources["langdetect"])
    if projects is not None:
        (payload/PROJECTS).mkdir(parents=True,exist_ok=False)
        for name,raw in projects.items():
            member=PROJECTS+'/'+name+'.tar.gz'
            with (payload/member).open('xb') as stream:
                stream.write(raw)
            result[member]=_sha(raw)
    with (payload/MEMBER).open("xb") as stream:
        stream.write(report)
    return result


def verify_native_plugin_inputs(payload: Path,manifest: dict,bot_relative: str,members=None) -> dict | None:
    if members is None:
        members=[MEMBER] if (payload/MEMBER).exists() else []
        for directory in ("plugin-wheels","plugin-sources","plugin-projects"):
            if (payload/directory).exists():
                members.extend(path.relative_to(payload).as_posix() for path in (payload/directory).rglob("*") if path.is_file() or path.is_symlink())
    present={name for name in members if reserved_member(name)}
    if _policy(payload,manifest,bot_relative) is None:
        if present:
            raise ValueError("Plugin artifacts lack their own frozen source policy")
        return None
    if MEMBER not in present or any(
            name.startswith("plugin-wheels/") and not name.startswith(WHEELS+"/")
            or name.startswith("plugin-sources/") and not name.startswith(SOURCES+"/")
            or name.startswith("plugin-projects/") and not name.startswith(PROJECTS+"/") for name in present):
        raise ValueError("Plugin source policy requires its report and exact original wheel namespace")
    expected=build_plugin_inputs(payload,manifest,bot_relative)
    wheel_names={name for name in present if name.startswith(WHEELS+'/')}
    source_names={name for name in present if name.startswith(SOURCES+'/')}
    project_names={name for name in present if name.startswith(PROJECTS+'/')}
    projects=_cached_projects(payload/PROJECTS,_json(expected)) if project_names else None
    wheels=None
    sources=None
    expected_members={MEMBER}
    if wheel_names:
        wheels=_cached(payload/WHEELS,_json(expected))
        sources=_cached_sources(payload/SOURCES if source_names else None,_json(expected))
        expected=build_plugin_inputs(payload,manifest,bot_relative,tuple(wheels.values()),source_inputs=sources)
        expected_members.update(WHEELS+'/'+name for name in wheels)
        expected_members.update(SOURCES+'/'+SOURCE_FILENAME for _name in sources)
    if projects is not None:
        expected=build_plugin_inputs(payload,manifest,bot_relative,tuple(wheels.values()) if wheels is not None else None,
            source_inputs=sources,project_inputs=projects)
        expected_members.update(PROJECTS+'/'+name+'.tar.gz' for name in projects)
    if present!=expected_members:
        raise ValueError("Plugin artifact members differ from the complete original input namespaces")
    if _read(payload/MEMBER,payload,4*1024*1024)!=expected:
        raise ValueError("Plugin input report differs from its complete original source declarations and wheels")
    return _json(expected)


def _project_declarations(payload,manifest,policy,upstream,artifacts):
    declarations={}
    for name,repository in (('backend','Open-LLM-VTuber'),('frontend','Open-LLM-VTuber-Web')):
        original=upstream[name]
        artifact=artifacts[name]
        if (type(original) is not dict or set(original)!={'repository','revision','patch'}
                or original['repository']!='https://github.com/Open-LLM-VTuber/'+repository+'.git'
                or type(original['revision']) is not str or re.fullmatch(r'[a-f0-9]{40}',original['revision']) is None
                or original['patch']!='patches/'+name+'.patch'
                or type(artifact) is not dict or set(artifact)!={'url','sha256'}
                or artifact['url']!='https://codeload.github.com/Open-LLM-VTuber/'+repository+'/tar.gz/'+original['revision']
                or type(artifact['sha256']) is not str or re.fullmatch(r'[a-f0-9]{64}',artifact['sha256']) is None):
            raise ValueError('Plugin project declarations differ from the explicit original Git source policy')
        patch_path=_RESOURCE+original['patch']
        _frozen(payload,manifest,patch_path)
        declarations[name]={'archive_sha256':artifact['sha256'],'revision':original['revision'],
            'patch_path':patch_path,'patch_sha256':manifest[patch_path]['sha256'],
            'source_root':repository+'-'+original['revision'],'target_root':'runtime/apps/'+name,
            **policy['projects'][name]}
    return declarations


def _project_pins(declaration):
    return {'expected_archive_sha256':declaration['archive_sha256'],'expected_revision':declaration['revision'],
        'expected_patch_sha256':declaration['patch_sha256'],'source_root':declaration['source_root'],
        'target_root':declaration['target_root'],'recipe':declaration['recipe'],
        'allowed_cjk_paths':tuple(declaration['allowed_cjk_paths'])}


def _cached_projects(cache,report):
    declarations=report.get('project_declarations')
    if not declarations:
        raise ValueError('Plugin projects lack their independently frozen project policy')
    paths=tuple(cache.iterdir())
    if {path.name for path in paths}!={name+'.tar.gz' for name in declarations} or len(paths)!=len(declarations):
        raise ValueError('Plugin project cache is missing or contains extra members')
    return {name:_read(cache/(name+'.tar.gz'),cache,64*1024*1024,row['archive_sha256'])
        for name,row in declarations.items()}


def stage_native_plugin_projects(payload: Path, manifest: dict, bot_relative: str, *, workspace: Path) -> dict:
    """Production input boundary into an empty private lab; never a live installation."""
    from chatcopilot.core.project_materialize import ProjectSourceInput, materialize_project_sources_isolated
    verified=verify_native_plugin_inputs(payload,manifest,bot_relative)
    if verified is None or not verified.get('project_sources_verified'):
        raise ValueError('Plugin project staging requires both authenticated original source archives')
    originals=_cached_projects(payload/PROJECTS,verified)
    inputs=tuple(ProjectSourceInput(originals[name],_frozen(payload,manifest,row['patch_path']),**_project_pins(row))
        for name,row in verified['project_declarations'].items())
    return asdict(materialize_project_sources_isolated(inputs,workspace=workspace))
