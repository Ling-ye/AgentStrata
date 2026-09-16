import pytest

from chatcopilot.harness.health_documentation import classify, documentation_path
from chatcopilot.harness.health_policy import documentation_file, policy_path, scan_path, scope_path


def fixture(tmp_path, old, new, name="src/sample/__init__.py", extra=None):
    before, after = tmp_path / "before", tmp_path / "after"
    files = {name: {"executable": False}, **{n: {"executable": False} for n in extra or {}}}
    for root, text in ((before, old), (after, new)):
        for n, content in {name: text, **(extra or {})}.items():
            path = root / n
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    return classify(before, after, [name], files, files)


@pytest.mark.parametrize("old,new", [
    ('"""Old."""\nvalue = 1\n', '"""Current multi-line\npackage description."""\nvalue = 1\n'),
    ("# Old comment\nvalue = 1\n", "# Current comment\nvalue = 1\n"),
])
def test_ordinary_python_description_and_comments(tmp_path, old, new):
    assert fixture(tmp_path, old, new)["eligible"]


@pytest.mark.parametrize("old,new", [
    ("value = 1\n", "value = 2\n"),
    ("value = 1 # noqa: F841\n", "value = 1 # noqa\n"),
    ("# coding: utf-8\nvalue = 1\n", "# coding: ascii\nvalue = 1\n"),
    ("# fmt: off\nvalue = 1\n", "# fmt: on\nvalue = 1\n"),
    ('def f():\n    """Old."""\n    return 1\n', 'def f():\n    """New."""\n    return 1\n'),
    ('class C:\n    """Old."""\n', 'class C:\n    """New."""\n'),
    ('"""Old."""\n', '"""New."""\nprint("extra")\n'),
])
def test_executable_and_directive_changes_require_standard_proof(tmp_path, old, new):
    assert not fixture(tmp_path, old, new)["eligible"]


@pytest.mark.parametrize("consumer", [
    "import sample\nhelp_text = sample.__doc__\n",
    "import sample as package\nhelp_text = package.__doc__\n",
    "from sample import __doc__ as description\n",
    "import inspect, sample\nhelp_text = inspect.getdoc(sample)\n",
    "import sample\nhelp_text = getattr(sample, '__doc__')\n",
    "from . import sample\nhelp_text = sample.__doc__\n",
])
def test_module_doc_consumers_require_standard_verification(tmp_path, consumer):
    assert not fixture(tmp_path, '"""Old."""\n', '"""New."""\n', extra={"src/consumer.py": consumer})["eligible"]


def test_module_own_cli_help_is_not_plain_documentation(tmp_path):
    assert not fixture(tmp_path, '"""Old."""\nhelp_text = __doc__\n',
                       '"""New."""\nhelp_text = __doc__\n')["eligible"]


@pytest.mark.parametrize("name", ["docs/reference/agent.md", "specs/example/spec.md", "tests/test_example.py",
    "src/chatcopilot/harness/models.py", "src/chatcopilot/agent/context/prompt_plan.py",
    "src/chatcopilot/authorization/example.py", "src/chatcopilot/core/source_snapshot.py", "scripts/check_example.py"])
def test_authoritative_and_runtime_material_is_not_lightweight(name):
    assert not documentation_path(name)


@pytest.mark.parametrize("name", ["AGENTS.md", "SECURITY.md", "CODE_OF_CONDUCT.md", ".cursor/rules/runtime.mdc", "docs/maintenance.md",
                                  ".github/pull_request_template.md", "docs/reference/runtime.md", "specs/runtime/spec.md"])
def test_documentation_scan_includes_frozen_authority(name):
    assert documentation_file(name)
    assert scan_path(name, "docs")
    assert policy_path(name)
    assert not documentation_path(name)


@pytest.mark.parametrize("name", ["README.md", "CONTRIBUTING.md", "docs/guides/run.md", "deploy/README.md"])
def test_ordinary_documentation_can_be_repaired(name):
    assert scan_path(name, "docs") and scope_path(name, "docs")
    assert documentation_path(name) and not policy_path(name)


def test_added_and_deleted_markdown_use_global_document_checks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    record = {"executable": False}
    assert classify(root, root, ["docs/guides/new.md"], {}, {"docs/guides/new.md": record})["eligible"]
    assert classify(root, root, ["docs/guides/old.md"], {"docs/guides/old.md": record}, {})["eligible"]
    assert not classify(root, root, ["docs/reference/rule.md"], {"docs/reference/rule.md": record}, {})["eligible"]
    executable = {"docs/guides/script.md": {"executable": True}}
    assert not classify(root, root, list(executable), executable, executable)["eligible"]
