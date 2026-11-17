"""Close original source-pinned MCP declarations for an explicit Windows CPython target."""
from __future__ import annotations

from collections import deque
import re

import packaging
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name
from packaging.version import Version

from chatcopilot.core.wheel_plan import locked_wheel_artifact

_REGISTRY={"registry":"https://pypi.org/simple"}


def _requirement(text):
    if type(text) is not str or not text.isascii() or not 0<len(text)<=4096 or text.count("(")>16:
        raise ValueError("MCP requirement exceeds syntax budget")
    try:
        value=Requirement(text)
    except (ValueError,RecursionError):
        raise ValueError("Malformed MCP requirement") from None
    if value.url is not None:
        raise ValueError("MCP dependencies require a separate direct-source policy")
    return value


def locked_dependency_inputs(project,lock,version):
    """Close upstream lock declarations for explicit regular CPython 3.14, not wheel METADATA or native ABI."""
    if type(version) is not str or re.fullmatch(r"3\.14\.[0-9]{1,4}",version) is None:
        raise ValueError("MCP declarations require their explicit CPython 3.14 target")
    environment={"implementation_name":"cpython","implementation_version":version,"os_name":"nt","sys_platform":"win32",
        "platform_machine":"AMD64","platform_python_implementation":"CPython","platform_system":"Windows",
        "platform_release":"","platform_version":"","python_full_version":version,"python_version":"3.14","extra":""}
    def active(text):
        if text is None:
            return True
        if (type(text) is not str or len(text)>4096 or text.count("(")>16
                or re.search(r"\b(platform_release|platform_version|extra|extras|dependency_groups)\b",text)):
            raise ValueError("MCP marker requires unsupported target facts")
        try:
            return Marker(text).evaluate(environment=environment,context="metadata")
        except (ValueError,KeyError,RecursionError):
            raise ValueError("MCP marker is unsupported") from None
    declared=project["project"]
    roots=declared["dependencies"]
    uv=project.get("tool",{}).get("uv",{})
    if (declared.get("dynamic") or type(roots) is not list or not 1<=len(roots)<=128
            or set(uv)-{"constraint-dependencies","extra-build-dependencies"}
            or lock.get("version")!=1 or lock.get("requires-python")!=declared["requires-python"]
            or not SpecifierSet(declared["requires-python"]).contains(version)):
        raise ValueError("MCP project or lock requires an unsupported resolution policy")
    packages=lock["package"]
    if type(packages) is not list or not 1<=len(packages)<=512:
        raise ValueError("MCP lock package budget exceeded")
    local=[row for row in packages if row.get("source")=={"editable":"."}]
    if len(local)!=1 or local[0]["name"]!=declared["name"] or local[0]["version"]!=declared["version"]:
        raise ValueError("MCP lock does not bind its one source project")
    for row in packages:
        Version(row["version"])
        if row not in local and row.get("source")!=_REGISTRY:
            raise ValueError("MCP lock has an unadmitted package source")
    default_metadata=[row for row in local[0]["metadata"]["requires-dist"] if "marker" not in row]
    source_roots=[_requirement(text) for text in roots]
    if sorted(str(item) for item in source_roots)!=sorted(str(_requirement(row["name"]+row.get("specifier",""))) for row in default_metadata):
        raise ValueError("MCP source roots differ from their locked declarations")
    constraints=[_requirement(text) for text in uv.get("constraint-dependencies",[])]
    if sorted((item.name,str(item.specifier)) for item in constraints)!=sorted((row["name"],row.get("specifier","")) for row in lock.get("manifest",{}).get("constraints",[])):
        raise ValueError("MCP resolution constraints differ from their source")
    nodes: dict[str,dict]={}
    extras: dict[str,set[str]]={}
    processed: dict[str,frozenset[str]]={}
    pending: deque[str]=deque()
    edges=set()
    def choose(name):
        matches=[row for row in packages if canonicalize_name(row["name"])==name and row not in local
            and (not row.get("resolution-markers") or any(active(marker) for marker in row["resolution-markers"]))]
        if len(matches)!=1:
            raise ValueError("MCP dependency does not select exactly one locked version: "+name)
        return matches[0]
    def enqueue(parent,edge):
        if type(edge) is not dict or set(edge)-{"name","version","source","marker","extra"}:
            raise ValueError("Unsupported MCP lock edge")
        if not active(edge.get("marker")):
            return
        name=canonicalize_name(edge["name"],validate=True)
        row=choose(name)
        if (edge.get("version",row["version"])!=row["version"] or edge.get("source",_REGISTRY)!=_REGISTRY):
            raise ValueError("MCP edge differs from its selected source/version")
        requested=edge.get("extra",[])
        if type(requested) is not list or len(requested)>32 or any(type(item) is not str for item in requested):
            raise ValueError("MCP extras exceed their explicit set budget")
        requested={canonicalize_name(item,validate=True) for item in requested}
        nodes[name]=row
        added=name not in extras or not requested.issubset(extras[name])
        extras.setdefault(name,set()).update(requested)
        edges.add((parent,name,edge.get("marker",""),tuple(sorted(requested))))
        if len(edges)>4096:
            raise ValueError("MCP graph edge budget exceeded")
        if added:
            pending.append(name)
    name: str
    for root in source_roots:
        if root.marker and not active(str(root.marker)):
            continue
        name=canonicalize_name(root.name,validate=True)
        if not root.specifier.contains(choose(name)["version"],prereleases=True):
            raise ValueError("MCP root does not satisfy its frozen project")
        enqueue("project",{"name":name,"extra":sorted(root.extras)})
    while pending:
        name=pending.popleft()
        activated=frozenset(extras[name])
        if processed.get(name)==activated:
            continue
        processed[name]=activated
        row=nodes[name]
        optional=row.get("optional-dependencies",{})
        if not activated.issubset(optional):
            raise ValueError("MCP lock does not provide a requested extra")
        for edge in [*row.get("dependencies",[]),*(edge for extra in sorted(activated) for edge in optional[extra])]:
            enqueue(name,edge)
    for constraint in constraints:
        name=canonicalize_name(constraint.name)
        if name in nodes and not constraint.specifier.contains(nodes[name]["version"],prereleases=True):
            raise ValueError("MCP graph violates a source constraint")
    tags=list(cpython_tags((3,14),abis=["cp314"],platforms=["win_amd64"]))
    tags.extend(compatible_tags((3,14),interpreter="cp314",platforms=["win_amd64"]))
    ranks={str(tag):index for index,tag in enumerate(tags)}
    selected=[]
    blockers=[]
    for name,row in sorted(nodes.items()):
        candidates=[]
        seen=set()
        for wheel in row.get("wheels",[]):
            artifact,build,wheel_tags=locked_wheel_artifact(wheel,name,row["version"])
            if artifact["filename"] in seen:
                raise ValueError("MCP wheel filenames repeat")
            seen.add(artifact["filename"])
            overlap=[ranks[str(tag)] for tag in wheel_tags if str(tag) in ranks]
            if overlap:
                candidates.append((min(overlap),build,artifact))
        if not candidates:
            blockers.append("missing-compatible-wheel:"+name)
            continue
        rank=min(item[0] for item in candidates)
        build=max(item[1] for item in candidates if item[0]==rank)
        best=[item[2] for item in candidates if item[:2]==(rank,build)]
        if len(best)!=1:
            raise ValueError("MCP compatible wheel choice is ambiguous")
        selected.append(best[0])
    if sum(row["bytes"] for row in selected)>512*1024*1024:
        raise ValueError("MCP selected input bytes exceed budget")
    return {"scope":"upstream-locked-declarations","roots":roots,"python_version":version,"selected":selected,
        "nodes":[{"name":name,"version":row["version"],"extras":sorted(extras[name])} for name,row in sorted(nodes.items())],
        "edges":[{"source":parent,"target":name,"marker":marker,"extras":list(requested)} for parent,name,marker,requested in sorted(edges)],
        "blockers":sorted(blockers),"unselected_locked_packages":len(packages)-len(nodes)-1,
        "packaging_version":packaging.__version__,"wheel_metadata_verified":False,"native_abi_verified":False,
        "source_project_installation_verified":False}

