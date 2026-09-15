import pytest

from chatcopilot.harness.health_documentation import classify, documentation_path, markdown_links


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


@pytest.mark.parametrize("name", ["docs/ai-contracts-agent.md", "specs/example/spec.md", "tests/test_example.py",
    "src/chatcopilot/harness/models.py", "src/chatcopilot/agent/context/prompt_plan.py",
    "src/chatcopilot/authorization/example.py", "src/chatcopilot/core/source_snapshot.py", "scripts/check_example.py"])
def test_authoritative_and_runtime_material_is_not_lightweight(name):
    assert not documentation_path(name)


def test_markdown_local_links_and_references(tmp_path):
    root = tmp_path / "source"
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("Project")
    (root / "docs/guide.md").write_text("[home](../README.md#intro)\n[web](https://example.com)\n[local][one]\n[one]: ../README.md\n")
    assert markdown_links(root, ["docs/guide.md"]) == []
    (root / "docs/guide.md").write_text("[bad](missing.md)\n[out](../../outside.md)\n")
    assert len(markdown_links(root, ["docs/guide.md"])) == 2
