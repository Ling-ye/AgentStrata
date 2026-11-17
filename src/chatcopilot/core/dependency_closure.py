"""Check a frozen Windows wheel graph, without resolving, installing or importing it."""
from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, replace
import hashlib
import io
import re
import token
import tokenize
import zipfile

import packaging
from packaging.tags import compatible_tags, cpython_tags
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.python_plan import _archive_version, _version
from chatcopilot.core.wheel_plan import (
    PinnedWheel, WheelPlanLimits, WindowsWheelPlan, WindowsWheelInputInventory, audit_windows_wheel_inputs,
    audit_windows_mcp_wheel_inputs, audit_windows_plugin_wheel_inputs, _headers, _name, _values, windows_wheel_plan,
)


@dataclass(frozen=True)
class DependencyClosureLimits:
    max_roots: int = 128
    max_requirements: int = 4096
    max_requirement_bytes: int = 4096
    max_extras_per_wheel: int = 32
    max_evaluations: int = 32768
    max_depth: int = 16
    max_tokens: int = 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise InstallError("Dependency closure budgets require positive integers")


@dataclass(frozen=True)
class DependencyFact:
    source: str | None
    requirement_sha256: str
    name: str
    specifier: str
    requested_extras: tuple[str, ...]
    marker_sha256: str | None
    active: bool


@dataclass(frozen=True)
class DependencyNode:
    name: str
    version: str
    archive_sha256: str
    metadata_sha256: str
    requires_python: str | None
    provided_extras: tuple[str, ...]
    activated_extras: tuple[str, ...]
    dynamic_fields: tuple[str, ...]
    external_requirement_sha256: tuple[str, ...]


@dataclass(frozen=True)
class WindowsDependencyClosure:
    python_version: str
    marker_environment: tuple[tuple[str, str], ...]
    nodes: tuple[DependencyNode, ...]
    requirements: tuple[DependencyFact, ...]
    scope: str = "pinned-python-distributions"
    native_abi_verified: bool = False
    recipe: str = "windows-python-dependency-closure/v1"
    schema: str = "agentstrata-windows-dependency-closure/v1"


@dataclass(frozen=True)
class WindowsDependencyPlan:
    wheels: WindowsWheelPlan
    closure: WindowsDependencyClosure


@dataclass(frozen=True)
class _Extra:
    name: str


@dataclass(frozen=True)
class _Requirement:
    parsed: Requirement
    name: str
    extras: tuple[str, ...]
    sha256: str
    marker_sha256: str | None
    marker_tokens: tuple[str | _Extra, ...]


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _text(value, limits):
    if type(value) is not str:
        raise InstallError("Dependency metadata must contain text")
    value = re.sub(r"\n[ \t]+", " ", value.replace("\r\n", "\n")).strip()
    if (not value or not value.isascii() or "\r" in value or "\n" in value
            or len(value) > limits.max_requirement_bytes):
        raise InstallError("Dependency metadata exceeds its text or encoding policy")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(value).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        raise InstallError("Dependency metadata tokenization is unsupported") from None
    depth = 0
    for item in tokens:
        if item.type == token.OP and item.string in {"(", "[", "{"}:
            depth += 1
        elif item.type == token.OP and item.string in {")", "]", "}"}:
            depth -= 1
        if depth < 0 or depth > limits.max_depth:
            raise InstallError("Dependency metadata nesting exceeds its budget")
    if depth or len(tokens) > limits.max_tokens:
        raise InstallError("Dependency metadata structure exceeds its budget")
    return value


def _environment(version, *, mcp=False):
    major, minor, _ = _archive_version(version,"windows_mcp_python") if mcp else _version(version)
    return {"implementation_name": "cpython", "implementation_version": version,
        "os_name": "nt", "platform_machine": "AMD64", "platform_python_implementation": "CPython",
        "platform_system": "Windows", "python_full_version": version, "python_version": f"{major}.{minor}",
        "sys_platform": "win32", "platform_release": "", "platform_version": "", "extra": ""}


def _marker_tokens(marker, *, root, environment, limits):
    text = _text(str(marker), limits)
    items = [item for item in tokenize.generate_tokens(io.StringIO(text).readline)
             if item.type not in {token.ENDMARKER, token.NEWLINE, tokenize.NL}]
    output: list[str | _Extra] = []
    index = 0
    supported = set(environment) - {"platform_release", "platform_version"}
    while index < len(items):
        item = items[index]
        if item.type == token.NAME and item.string not in supported | {"and", "or", "in", "not"}:
            raise InstallError("Dependency marker requires an undefined target environment field")
        forward = (item.type == token.NAME and item.string == "extra")
        reverse = (item.type == token.STRING and index + 2 < len(items)
                   and items[index + 1].string in {"==", "!=", "<", ">", "<=", ">=", "~=", "===", "in"}
                   and items[index + 2].type == token.NAME and items[index + 2].string == "extra")
        if forward or reverse:
            if (root or index + 2 >= len(items) or items[index + 1].string != "=="
                    or (forward and items[index + 2].type != token.STRING)):
                raise InstallError("Dependency extra markers require positive named equality in wheel metadata")
            literal = items[index + 2].string if forward else item.string
            try:
                name = _name(ast.literal_eval(literal))
            except (ValueError, SyntaxError, TypeError):
                raise InstallError("Dependency extra marker name is invalid") from None
            output.append(_Extra(name))
            index += 3
        else:
            output.append(item.string)
            index += 1
    return tuple(output)


def _requirement(value, *, root, environment, limits):
    text = _text(value, limits)
    try:
        parsed = Requirement(text)
    except (ValueError, RecursionError):
        raise InstallError("Invalid frozen dependency requirement") from None
    # Never resolve a URL, editable source, local path or index fallback.
    if parsed.url is not None:
        raise InstallError("Direct dependency references require a separate provenance policy")
    extras = tuple(sorted({_name(name) for name in parsed.extras}))
    if len(extras) > limits.max_extras_per_wheel:
        raise InstallError("Dependency extras exceed their budget")
    tokens = _marker_tokens(parsed.marker, root=root, environment=environment, limits=limits) if parsed.marker else ()
    marker_sha = _sha(str(parsed.marker).encode()) if parsed.marker else None
    return _Requirement(parsed, _name(parsed.name), extras, _sha(text.encode()), marker_sha, tokens)


def _active(requirement, extras, environment):
    if not requirement.marker_tokens:
        return True
    # A named extra comparison is membership in the package's complete requested
    # set, including comparisons joined by AND. Other marker semantics stay in
    # packaging's public evaluator with every target field explicitly overridden.
    pieces = [f"(os_name {'==' if item.name in extras else '!='} 'nt')" if isinstance(item, _Extra) else item
              for item in requirement.marker_tokens]
    try:
        return Marker(" ".join(pieces)).evaluate(environment=environment, context="metadata")
    except (ValueError, KeyError, TypeError, RecursionError):
        raise InstallError("Dependency marker comparison is unsupported for the frozen target") from None


def _closure(pins, plan, version, roots, limits, wheel_limits, *, mcp=False):
    environment = _environment(version,mcp=mcp)
    if type(roots) is not tuple or not 0 < len(roots) <= limits.max_roots:
        raise InstallError("Dependency closure requires bounded immutable root requirements")
    root_requirements = tuple(_requirement(value, root=True, environment=environment, limits=limits) for value in roots)
    by_hash = {pin.sha256: pin.data for pin in pins}
    nodes, dependencies = {}, {}
    count = len(roots)
    for wheel in plan.wheels:
        with zipfile.ZipFile(io.BytesIO(by_hash[wheel.archive_sha256])) as source:
            raw = source.read(wheel.dist_info + "/METADATA")
        if len(raw) > wheel_limits.max_metadata_bytes:
            raise InstallError("Dependency metadata exceeds its byte budget")
        metadata = _headers(raw)
        if any(_values(metadata, key) for key in ("Requires", "Provides", "Obsoletes", "Provides-Dist", "Obsoletes-Dist")):
            raise InstallError("Legacy or virtual distribution relationships require an explicit policy")
        python_fields = _values(metadata, "Requires-Python")
        if len(python_fields) > 1:
            raise InstallError("Requires-Python is repeated")
        constraint = _text(python_fields[0], limits) if python_fields else None
        try:
            if constraint is not None and not SpecifierSet(constraint).contains(Version(version), prereleases=True):
                raise InstallError("Selected wheel excludes the pinned target Python")
        except ValueError:
            raise InstallError("Selected wheel has an invalid or incompatible Requires-Python") from None
        provided = tuple(_name(_text(value, limits)) for value in _values(metadata, "Provides-Extra"))
        if len(provided) != len(set(provided)) or len(provided) > limits.max_extras_per_wheel:
            raise InstallError("Provided dependency extras are ambiguous or exceed their budget")
        dynamic = tuple(sorted({_text(value, limits).lower() for value in _values(metadata, "Dynamic")}))
        if any(not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", field) for field in dynamic):
            raise InstallError("Dynamic metadata field name is invalid")
        external = tuple(sorted(_sha(_text(value, limits).encode()) for value in _values(metadata, "Requires-External")))
        raw_requirements = _values(metadata, "Requires-Dist")
        count += len(raw_requirements) + len(external)
        if count > limits.max_requirements:
            raise InstallError("Dependency requirement set exceeds its budget")
        dependencies[wheel.name] = tuple(_requirement(value, root=False, environment=environment, limits=limits) for value in raw_requirements)
        nodes[wheel.name] = DependencyNode(wheel.name, wheel.version, wheel.archive_sha256, _sha(raw), constraint,
                                          tuple(sorted(provided)), (), dynamic, external)
    activated: dict[str, set[str]] = {name: set() for name in nodes}
    reached, queued = set(), set()
    pending: deque[str] = deque()
    evaluations = 0

    def evaluate(requirement, extras):
        nonlocal evaluations
        evaluations += 1
        if evaluations > limits.max_evaluations:
            raise InstallError("Dependency closure evaluation exceeds its budget")
        return _active(requirement, extras, environment)

    def require(requirement):
        name = requirement.name
        if name not in nodes:
            raise InstallError("Dependency is absent from the frozen wheel set: " + name)
        try:
            matches = requirement.parsed.specifier.contains(Version(nodes[name].version), prereleases=True)
        except ValueError:
            raise InstallError("Frozen dependency version is unsupported") from None
        if not matches:
            raise InstallError("Selected dependency violates a frozen version constraint: " + name)
        if not set(requirement.extras).issubset(nodes[name].provided_extras):
            raise InstallError("Requested dependency extra is not provided: " + name)
        changed = name not in reached or not set(requirement.extras).issubset(activated[name])
        reached.add(name)
        activated[name].update(requirement.extras)
        if changed and name not in queued:
            queued.add(name)
            pending.append(name)

    for requirement in root_requirements:
        if evaluate(requirement, set()):
            require(requirement)
    while pending:
        name = pending.popleft()
        queued.remove(name)
        for requirement in dependencies[name]:
            if evaluate(requirement, activated[name]):
                require(requirement)
    if reached != nodes.keys():
        raise InstallError("Frozen wheel set contains a distribution unreachable from active roots")

    def fact(source, requirement, extras):
        return DependencyFact(source, requirement.sha256, requirement.name, str(requirement.parsed.specifier),
                              requirement.extras, requirement.marker_sha256, evaluate(requirement, extras))

    facts = [fact(None, requirement, set()) for requirement in root_requirements]
    for name in sorted(nodes):
        facts.extend(fact(name, requirement, activated[name]) for requirement in dependencies[name])
    output = tuple(DependencyNode(node.name, node.version, node.archive_sha256, node.metadata_sha256, node.requires_python,
        node.provided_extras, tuple(sorted(activated[node.name])), node.dynamic_fields, node.external_requirement_sha256)
        for node in sorted(nodes.values(), key=lambda node: node.name))
    target = tuple(sorted((key, value) for key, value in environment.items() if key not in {"extra", "platform_release", "platform_version"}))
    return WindowsDependencyClosure(version, target, output,
        tuple(sorted(facts, key=lambda item: (item.source or "", item.name, item.requirement_sha256))))


def windows_dependency_plan(inputs: tuple[PinnedWheel, ...], *, python_version: str,
                            requirements: tuple[str, ...], limits: DependencyClosureLimits = DependencyClosureLimits(),
                            wheel_limits: WheelPlanLimits = WheelPlanLimits()) -> WindowsDependencyPlan:
    """Validate original host-pinned bytes and named Python dependency closure.

    Pins, roots and the target version must come from independently frozen host
    inputs. This is a validator of an exact set, not a resolver or installation
    receipt. External libraries, native ABI and runtime behavior remain unverified.
    """
    # A compatibility refusal, not proof of host code provenance. Production must
    # independently bind this validator and its dependencies in the trusted host.
    if getattr(packaging, "__version__", None) != "26.3":
        raise InstallError("Dependency checking requires the pinned packaging implementation")
    if type(limits) is not DependencyClosureLimits or type(wheel_limits) is not WheelPlanLimits:
        raise InstallError("Dependency planning requires explicit bounded policies")
    major, minor, _ = _version(python_version)
    wheels = windows_wheel_plan(inputs, compatible_tags=(f"cp{major}{minor}-cp{major}{minor}-win_amd64", "py3-none-any"), limits=wheel_limits)
    return WindowsDependencyPlan(wheels, _closure(inputs, wheels, python_version, requirements, limits, wheel_limits))



def windows_python_tags(python_version: str) -> tuple[str, ...]:
    """Explicit ordinary CPython Windows x64 tags; never inspect the verifier ABI."""
    if getattr(packaging, "__version__", None) != "26.3":
        raise InstallError("Dependency checking requires the pinned packaging implementation")
    major, minor, _ = _version(python_version)
    interpreter = f"cp{major}{minor}"
    tags = list(cpython_tags((major, minor), abis=[interpreter], platforms=["win_amd64"]))
    tags.extend(compatible_tags((major, minor), interpreter=interpreter, platforms=["win_amd64"]))
    return tuple(dict.fromkeys(str(tag) for tag in tags))


def windows_mcp_python_tags(python_version: str) -> tuple[str, ...]:
    """Dedicated ordinary MCP CPython 3.14 tags; do not widen host/venv policy."""
    if getattr(packaging, "__version__", None) != "26.3":
        raise InstallError("Dependency checking requires the pinned packaging implementation")
    major, minor, _ = _archive_version(python_version, "windows_mcp_python")
    interpreter = f"cp{major}{minor}"
    tags = list(cpython_tags((major, minor), abis=[interpreter], platforms=["win_amd64"]))
    tags.extend(compatible_tags((major, minor), interpreter=interpreter, platforms=["win_amd64"]))
    return tuple(dict.fromkeys(str(tag) for tag in tags))


def windows_requirement_fact(value: str, *, python_version: str,
                             limits: DependencyClosureLimits = DependencyClosureLimits()) -> DependencyFact:
    windows_python_tags(python_version)  # Also enforce the parser compatibility policy.
    if type(limits) is not DependencyClosureLimits:
        raise InstallError("Dependency planning requires explicit bounded policies")
    environment = _environment(python_version)
    requirement = _requirement(value, root=True, environment=environment, limits=limits)
    return DependencyFact(None, requirement.sha256, requirement.name, str(requirement.parsed.specifier),
        requirement.extras, requirement.marker_sha256, _active(requirement, set(), environment))


@dataclass(frozen=True)
class WindowsDependencyInputInventory:
    inventory: WindowsWheelInputInventory
    closure: WindowsDependencyClosure


def windows_dependency_input_inventory(inputs: tuple[PinnedWheel, ...], *, python_version: str,
                                      requirements: tuple[str, ...],
                                      limits: DependencyClosureLimits = DependencyClosureLimits(),
                                      wheel_limits: WheelPlanLimits = WheelPlanLimits()) -> WindowsDependencyInputInventory:
    """Check complete archive inputs and declared closure, without an install plan."""
    if type(limits) is not DependencyClosureLimits or type(wheel_limits) is not WheelPlanLimits:
        raise InstallError("Dependency planning requires explicit bounded policies")
    tags = windows_python_tags(python_version)
    inventory = audit_windows_wheel_inputs(inputs, compatible_tags=tags, limits=wheel_limits)
    return WindowsDependencyInputInventory(inventory,
        _closure(inputs, inventory, python_version, requirements, limits, wheel_limits))


def windows_plugin_dependency_input_inventory(inputs: tuple[PinnedWheel,...], *, python_version: str,
                                             requirements: tuple[str,...],
                                             limits: DependencyClosureLimits=DependencyClosureLimits(max_roots=256,max_extras_per_wheel=64),
                                             wheel_limits: WheelPlanLimits=WheelPlanLimits()) -> WindowsDependencyInputInventory:
    """Frozen plugin Python-3.12 inputs; no installation, prefix/loading or ABI authority."""
    if (getattr(packaging,'__version__',None)!='26.3' or type(python_version) is not str
            or re.fullmatch(r'3\.12\.[0-9]{1,4}',python_version) is None):
        raise InstallError('Plugin input checking requires pinned packaging and explicit Python 3.12')
    if (type(limits) is not DependencyClosureLimits or type(wheel_limits) is not WheelPlanLimits
            or limits.max_roots>256 or limits.max_extras_per_wheel>64):
        raise InstallError('Plugin input checking requires explicit bounded policies')
    inventory=audit_windows_plugin_wheel_inputs(inputs,compatible_tags=windows_python_tags(python_version),limits=wheel_limits)
    closure=_closure(inputs,inventory,python_version,requirements,limits,wheel_limits)
    return WindowsDependencyInputInventory(inventory,replace(closure,scope='pinned-windows-plugin-distributions',
        recipe='windows-plugin-python-dependency-closure/v1',schema='agentstrata-windows-plugin-dependency-closure/v1'))


def windows_mcp_dependency_input_inventory(inputs: tuple[PinnedWheel,...], *, python_version: str,
                                          requirements: tuple[str,...],
                                          limits: DependencyClosureLimits=DependencyClosureLimits(),
                                          wheel_limits: WheelPlanLimits=WheelPlanLimits()) -> WindowsDependencyInputInventory:
    """Dedicated regular MCP CPython 3.14 archive/metadata scope; never enable host or venv/ABI support."""
    if getattr(packaging,"__version__",None)!="26.3":
        raise InstallError("Dependency checking requires the pinned packaging implementation")
    if type(limits) is not DependencyClosureLimits or type(wheel_limits) is not WheelPlanLimits:
        raise InstallError("Dependency planning requires explicit bounded policies")
    tags=windows_mcp_python_tags(python_version)
    inventory=audit_windows_mcp_wheel_inputs(inputs,compatible_tags=tags,limits=wheel_limits)
    closure=_closure(inputs,inventory,python_version,requirements,limits,wheel_limits,mcp=True)
    closure=replace(closure,scope="pinned-windows-mcp-distributions",recipe="windows-mcp-python-dependency-closure/v1",
        schema="agentstrata-windows-mcp-dependency-closure/v1")
    return WindowsDependencyInputInventory(inventory,closure)
