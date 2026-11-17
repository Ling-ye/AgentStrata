"""Pinned sets must satisfy their declared Windows roots without ambient providers."""
from __future__ import annotations

import base64
import csv
from dataclasses import asdict, FrozenInstanceError
import io
import json
import stat
import subprocess
import zipfile

import packaging.markers
import pytest

from chatcopilot.core import dependency_closure as module
from chatcopilot.core.dependency_closure import DependencyClosureLimits, windows_dependency_plan
from chatcopilot.core.install_artifacts import InstallError
from chatcopilot.core.wheel_plan import PinnedWheel


def wheel(name, version="1.0", *, requires=(), extras=(), python=None, extra_headers=""):
    dist = name.replace("-", "_") + "-" + version + ".dist-info"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    metadata += "".join("Provides-Extra: " + extra + "\n" for extra in extras)
    metadata += "".join("Requires-Dist: " + value + "\n" for value in requires)
    if python is not None:
        metadata += "Requires-Python: " + python + "\n"
    metadata += extra_headers + "\n"
    files = {name.replace("-", "_") + "/__init__.py": b"raise RuntimeError('must not import')\n",
             dist + "/METADATA": metadata.encode(),
             dist + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n"}
    rows = [[path, "sha256=" + base64.urlsafe_b64encode(module.hashlib.sha256(data).digest()).rstrip(b"=").decode(), str(len(data))]
            for path, data in files.items()] + [[dist + "/RECORD", "", ""]]
    stream = io.StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerows(rows)
    files[dist + "/RECORD"] = stream.getvalue().encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(path, (2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    data = output.getvalue()
    return PinnedWheel(data, module.hashlib.sha256(data).hexdigest(), name, version)


def closed(*wheels, roots=("app==1.0",), version="3.13.15", **options):
    return windows_dependency_plan(tuple(wheels), python_version=version, requirements=roots, **options).closure


def test_transitive_closed_set_and_original_metadata_are_bound_without_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("closure executed a process"))
    pins = (wheel("app", requires=("dep>=2,<3",), python=">=3.12,<3.14"),
            wheel("dep", "2.1", requires=("leaf~=1.0",)), wheel("leaf", "1.2"))
    result = closed(*pins)
    assert [node.name for node in result.nodes] == ["app", "dep", "leaf"]
    assert {node.archive_sha256 for node in result.nodes} == {pin.sha256 for pin in pins}
    assert all(len(node.metadata_sha256) == 64 for node in result.nodes)
    assert result.scope == "pinned-python-distributions" and result.native_abi_verified is False
    assert {(fact.source, fact.name) for fact in result.requirements if fact.active} == {(None, "app"), ("app", "dep"), ("dep", "leaf")}
    assert result == closed(*reversed(pins)) and not list(tmp_path.iterdir())
    with pytest.raises(FrozenInstanceError):
        result.native_abi_verified = True


def test_cycle_and_full_set_extra_conjunction_reach_a_finite_fixed_point():
    pins = (wheel("app", extras=("foo", "baz"), requires=("bridge[bar]", "leaf; extra == 'foo' and extra == 'baz'")),
            wheel("bridge", extras=("bar",), requires=("app[baz]; 'bar' == extra",)), wheel("leaf"))
    result = closed(*pins, roots=("app[foo]==1.0",))
    nodes = {node.name: node for node in result.nodes}
    assert nodes["app"].activated_extras == ("baz", "foo") and nodes["bridge"].activated_extras == ("bar",)
    assert next(fact for fact in result.requirements if fact.name == "leaf").active
    assert result == closed(*reversed(pins), roots=("app[foo]==1.0",))


def test_unrequested_extras_remain_inactive_without_provider_fallback():
    result = closed(wheel("app", extras=("fast",), requires=("missing; extra == 'fast'",)))
    assert not next(fact for fact in result.requirements if fact.name == "missing").active
    with pytest.raises(InstallError, match="absent.*missing"):
        closed(wheel("app", extras=("fast",), requires=("missing; extra == 'fast'",)), roots=("app[fast]",))


@pytest.mark.parametrize("marker,base_active", [
    ("platform_python_implementation == 'CPython' and extra == 'fast'", False),
    ("os_name == 'nt' and extra == 'fast'", False),
    ("os_name == 'nt' or extra == 'fast'", True),
    ("extra == 'fast' and os_name == 'nt'", False),
    ("os_name == 'nt' and 'fast' == extra", False),
])
def test_boolean_adjacency_is_not_mistaken_for_a_reverse_extra_comparison(marker, base_active):
    app, dep = wheel("app", extras=("fast",), requires=("dep; " + marker,)), wheel("dep")
    base = closed(app, dep) if base_active else closed(app)
    assert next(fact for fact in base.requirements if fact.name == "dep").active == base_active
    enabled = closed(app, dep, roots=("app[fast]",))
    assert next(fact for fact in enabled.requirements if fact.name == "dep").active


def test_target_python_changes_active_provider_set_instead_of_using_the_host():
    app = wheel("app", requires=("old; python_version < '3.13'",))
    assert len(closed(app).nodes) == 1
    assert len(closed(app, wheel("old"), version="3.12.14").nodes) == 2
    with pytest.raises(InstallError, match="absent.*old"):
        closed(app, version="3.12.14")
    with pytest.raises(InstallError, match="unreachable"):
        closed(app, wheel("old"))


@pytest.mark.parametrize("marker", ["os_name == 'nt'", "sys_platform == 'win32'", "platform_system == 'Windows'",
    "platform_machine == 'AMD64'", "platform_python_implementation == 'CPython'", "implementation_name == 'cpython'",
    "implementation_version == '3.13.15'", "python_full_version == '3.13.15'", "python_version == '3.13'",
    "sys_platform in 'win32,linux'", "os_name not in 'posix'", "'win32' == sys_platform"])
def test_every_supported_target_field_overrides_poisoned_host_values(monkeypatch, marker):
    monkeypatch.setattr(packaging.markers, "default_environment", lambda: {key: "POISON" for key in module._environment("3.13.15")})
    result = closed(wheel("app", requires=("dep; " + marker,)), wheel("dep"))
    assert next(fact for fact in result.requirements if fact.name == "dep").active
    assert dict(result.marker_environment)["sys_platform"] == "win32"


@pytest.mark.parametrize("marker", ["platform_release == '11'", "platform_version == '10.0'", "'foo' in extras", "'foo' in dependency_groups"])
def test_unfrozen_or_out_of_context_marker_fields_are_rejected(marker):
    with pytest.raises(InstallError, match="undefined target"):
        closed(wheel("app", requires=("dep; " + marker,)))


@pytest.mark.parametrize("marker", ["extra != 'foo'", "extra in 'foo'", "extra not in 'foo'", "'foo' != extra", "extra == extra"])
def test_undefined_extra_operations_cannot_change_the_closure(marker):
    with pytest.raises(InstallError, match="positive named equality"):
        closed(wheel("app", requires=("dep; " + marker,), extras=("foo",)))


def test_extra_names_normalize_and_multiple_requesters_union_them():
    pins = (wheel("app", requires=("dep[Foo_Bar]", "bridge")),
        wheel("bridge", requires=("dep[other]",)),
        wheel("dep", extras=("foo-bar", "other"), requires=("leaf; extra == 'foo_bar' and extra == 'other'",)), wheel("leaf"))
    result = closed(*pins)
    assert next(node for node in result.nodes if node.name == "dep").activated_extras == ("foo-bar", "other")


@pytest.mark.parametrize("extras,root", [(("foo-bar", "foo_bar"), "app"), (("foo",), "app[missing]"), ((), "app[foo]")])
def test_ambiguous_or_undefined_extras_are_not_silently_ignored(extras, root):
    with pytest.raises(InstallError):
        closed(wheel("app", extras=extras), roots=(root,))


@pytest.mark.parametrize("constraint,valid", [(">=3.12,<3.14", True), ("~=3.12", True), ("==3.13.*", True),
    ("===3.13.15", True), ("!=3.13.15", False), ("<=3.12", False), ("not-a-specifier", False), ("", False)])
def test_requires_python_is_checked_against_the_pinned_version(constraint, valid):
    pin = wheel("app", python=constraint)
    if valid:
        assert closed(pin).nodes[0].requires_python
    else:
        with pytest.raises(InstallError):
            closed(pin)


@pytest.mark.parametrize("version,constraint,valid", [("2.1", ">=2,<3", True), ("2.1", "==2.*", True),
    ("2.1", "!=2.1", False), ("1!2.0", ">=1!1.0", True), ("1.0+local", "==1.0", True),
    ("2.0rc1", ">=2.0a1", True), ("2.0rc1", ">=2.0", False)])
def test_selected_versions_use_pep440_instead_of_string_order(version, constraint, valid):
    app, dep = wheel("app", requires=("dep" + constraint,)), wheel("dep", version)
    if valid:
        assert len(closed(app, dep).nodes) == 2
    else:
        with pytest.raises(InstallError, match="version constraint"):
            closed(app, dep)


@pytest.mark.parametrize("field", ["Requires", "Provides", "Obsoletes", "Provides-Dist", "Obsoletes-Dist"])
def test_unmodeled_distribution_relationships_fail_closed(field):
    with pytest.raises(InstallError, match="relationships"):
        closed(wheel("app", extra_headers=field + ": other\n"))


def test_repeated_python_constraint_and_invalid_dynamic_field_are_rejected():
    with pytest.raises(InstallError, match="repeated"):
        closed(wheel("app", python=">=3.9", extra_headers="Requires-Python: >=3.9\n"))
    with pytest.raises(InstallError, match="Dynamic"):
        closed(wheel("app", extra_headers="Dynamic: invalid field\n"))


def test_dynamic_annotations_use_exact_wheel_metadata_and_external_text_is_not_accepted_as_abi():
    result = closed(wheel("app", python=">=3.9", extra_headers="Dynamic: requires-python\nRequires-External: opaque-external-condition\n"))
    node = result.nodes[0]
    assert node.dynamic_fields == ("requires-python",) and len(node.external_requirement_sha256) == 1
    assert "opaque-external-condition" not in json.dumps(asdict(result)) and not result.native_abi_verified


def test_folded_requirement_headers_are_unfolded_before_standard_parsing():
    result = closed(wheel("app", requires=("dep >=1,\n <2; python_version >= '3.12'",)), wheel("dep"))
    assert next(fact for fact in result.requirements if fact.name == "dep").active


def test_direct_references_are_rejected_without_echoing_their_locations():
    from urllib.parse import urlunsplit
    requirement = "dep @ " + urlunsplit(("https", "example.invalid", "/private-token.whl", "", ""))
    for value in (requirement, requirement + "; os_name == 'posix'"):
        with pytest.raises(InstallError) as caught:
            closed(wheel("app", requires=(value,)))
        assert "private-token" not in str(caught.value)


@pytest.mark.parametrize("requirement", ["bad???", "../local/path", "-e app", "app; __import__('os')", "app;extra=='foo'"])
def test_root_requirements_are_bounded_declarations_not_commands_or_extra_contexts(requirement):
    with pytest.raises(InstallError):
        closed(wheel("app"), roots=(requirement,))


@pytest.mark.parametrize("roots", [[], (), (None,), ("app", "unused")])
def test_root_set_is_explicit_and_immutable(roots):
    with pytest.raises(InstallError):
        closed(wheel("app"), roots=roots)


@pytest.mark.parametrize("field", ["max_roots", "max_requirements", "max_requirement_bytes", "max_extras_per_wheel", "max_evaluations", "max_depth", "max_tokens"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "100"])
def test_resource_limit_types_and_values_are_strict(field, value):
    with pytest.raises(InstallError):
        DependencyClosureLimits(**{field: value})


@pytest.mark.parametrize("field", ["max_roots", "max_requirements", "max_requirement_bytes", "max_extras_per_wheel", "max_evaluations", "max_depth", "max_tokens"])
def test_actual_work_is_bounded_at_each_attack_surface(field):
    options = {"limits": DependencyClosureLimits(**{field: 1})}
    if field == "max_roots":
        pins, roots = (wheel("app"),), ("app", "app")
    elif field == "max_requirements":
        pins, roots = (wheel("app", requires=("dep",)), wheel("dep")), ("app",)
    elif field == "max_extras_per_wheel":
        pins, roots = (wheel("app", extras=("one", "two")),), ("app",)
    elif field == "max_depth":
        pins, roots = (wheel("app"),), ("app; ((os_name == 'nt'))",)
    else:
        pins, roots = (wheel("app"),), ("app==1.0",)
    with pytest.raises(InstallError):
        closed(*pins, roots=roots, **options)


@pytest.mark.parametrize("version", [None, "25.0", "26.2"])
def test_a_different_parser_implementation_cannot_silently_change_the_policy(monkeypatch, version):
    import packaging
    monkeypatch.setattr(packaging, "__version__", version)
    with pytest.raises(InstallError, match="pinned packaging"):
        closed(wheel("app"))
