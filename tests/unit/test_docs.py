from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def load_checker():
    path = Path(__file__).resolve().parents[2] / "scripts/check_docs.py"
    spec = importlib.util.spec_from_file_location("documentation_checker", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def checker():
    return load_checker()


def write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "README.md", "# Project\n\n[map](docs/README.md)\n")
    write(tmp_path, "docs/README.md", "# Tasks\n\n[guide](guides/run.md)\n[domain](reference/runtime.md)\n")
    write(tmp_path, "docs/guides/run.md", "# Run\n\nFollow the current public command.\n")
    write(tmp_path, "docs/reference/runtime.md", "# Runtime\n\n## 源码入口\n\n[code](../../src/runtime.py)\n")
    write(tmp_path, "src/runtime.py", "def run():\n    return 1\n")
    return tmp_path


def test_task_navigation_and_source_change_are_separate_from_failure(checker, repo):
    report = checker.check(repo, ("src/runtime.py",))
    assert report["violations"] == []
    assert [(x["rule"], x["path"], x["source_path"]) for x in report["review_hints"]] == [
        ("source-changed", "docs/reference/runtime.md", "src/runtime.py")]
    assert (repo / "docs/reference/runtime.md").read_text().startswith("# Runtime")


def test_directory_source_association_and_deleted_source(checker, repo):
    write(repo, "docs/reference/runtime.md", "# Runtime\n\n## 源码入口\n[code](../../src)\n")
    report = checker.check(repo, ("src/new.py",))
    assert not report["violations"]
    assert report["review_hints"][0]["source_path"] == "src/new.py"
    write(repo, "docs/reference/runtime.md", "# Runtime\n\n## 源码入口\n[code](../../src/missing.py)\n")
    assert {v["rule"] for v in checker.check(repo)["violations"]} == {"local-link", "source-entry"}


def test_reference_inline_image_html_and_unicode_anchors(checker, repo):
    write(repo, "docs/guides/图 (示例).png", "image fixture")
    write(repo, "docs/guides/run.md", """# Run

## 资源 `读取`
## 资源 `读取`
<a id="explicit"></a>
[first](#资源-读取) [second](#资源-读取-1) [anchor](#explicit)
![image](<图 (示例).png>)
[reference][code] [code][] [code]
[code]: ../../src/runtime.py "source"
<a href="../../src/runtime.py#L1-L2">source</a>
Literal `[bad](missing.md)` is an example.
```markdown
[bad](missing.md)
```
""")
    assert checker.check(repo)["violations"] == []


@pytest.mark.parametrize("content,rule", [
    ("[bad](missing.md)", "local-link"),
    ("[bad](#missing)", "anchor"),
    ("[bad][undefined]", "reference-link"),
    ("[code](../../src/runtime.py#L99)", "source-line"),
    ("[outside](../../../private.md)", "local-link"),
])
def test_actionable_locations(checker, repo, content, rule):
    write(repo, "docs/guides/run.md", "# Run\n\n" + content + "\n")
    report = checker.check(repo)
    assert any(v["rule"] == rule and v["path"] == "docs/guides/run.md" and v["line"] == 3 for v in report["violations"])


def test_orphan_addition_and_dangling_deletion(checker, repo):
    write(repo, "docs/guides/orphan.md", "# Unlinked\n")
    assert any(v["rule"] == "orphan" and v["path"].endswith("orphan.md") for v in checker.check(repo)["violations"])
    (repo / "docs/guides/run.md").unlink()
    assert any(v["rule"] == "local-link" and v["path"] == "docs/README.md" for v in checker.check(repo)["violations"])


@pytest.mark.parametrize("content", [
    "2026-09-16 本次实际验证：\n\n- pytest: 42 passed, 1 skipped\n",
    "## Latest execution\n\nThe local service is active.\n",
    "```bash\npytest -q\n# 42 passed\n```\n",
    "验证记录保存在 .cache/check-once/report.json\n",
])
def test_run_reports_are_rejected(checker, repo, content):
    write(repo, "docs/guides/run.md", "# Run\n\n" + content)
    assert any(v["rule"] == "run-record" for v in checker.check(repo)["violations"])


@pytest.mark.parametrize("content", [
    "---\ncreated: 2026-09-16\n---\n# Run\n\n刷新间隔为 2.5 秒。\n",
    "# Run\n\n## 0.2.0 - 2026-09-16\n\n新增参数校验。\n",
    "# Run\n\n## 反例\n\n不应把 `42 passed` 写成一次性的维护记录。\n",
    "# Run\n\n## 固定基准\n\n42 passed 是此示例的期望输出。\n",
    "# Run\n\n输入日期为 2026-06-15，验证器核对字段与单位。\n",
    "# Run\n\n```bash\npython scripts/check_repo.py docs\n```\n",
])
def test_versions_examples_and_thresholds_are_not_run_reports(checker, repo, content):
    write(repo, "docs/guides/run.md", content)
    assert checker.check(repo)["violations"] == []


def test_own_repository_main_links_are_checked_without_network(checker, repo):
    write(repo, "README.md", "# Root\n\n[map](https://github.com/Ling-ye/AgentStrata/blob/main/docs/README.md)\n[external](https://example.com/not-fetched)\n")
    assert checker.check(repo)["violations"] == []
    (repo / "docs/guides/run.md").unlink()
    assert checker.check(repo)["violations"]


def test_scope_excludes_runtime_prompts_licenses_and_data(checker, repo):
    for name in ("bots/demo/prompts/identity.md", "bots/demo/skills/demo/SKILL.md", "src/vendor/NOTICE.md", "src/fixtures/example.md"):
        write(repo, name, "42 passed\n[bad](missing.md)\n")
    assert checker.check(repo)["violations"] == []


def test_candidate_root_is_explicit_and_json_is_read_only(checker, repo, capsys):
    original = {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
    assert checker.main(["--root", str(repo), "--json", "--changed-path", "src/runtime.py"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source_links"]["docs/reference/runtime.md"]
    assert {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()} == original


def test_untrusted_changed_paths_and_symlinks(checker, repo, tmp_path):
    with pytest.raises(ValueError):
        checker.check(repo, ("../outside.py",))
    outside = tmp_path.parent / "outside-doc-fixture.md"
    outside.write_text("42 passed\n")
    (repo / "docs/guides/run.md").unlink()
    (repo / "docs/guides/run.md").symlink_to(outside)
    assert any(v["rule"] == "document-path" for v in checker.check(repo)["violations"])


def test_length_and_duplication_are_review_hints(checker, repo):
    paragraph = "Stable prose describing a reusable responsibility. " * 5
    write(repo, "docs/guides/run.md", "# Run\n\n" + paragraph + "\n\n" + "text\n" * 301)
    write(repo, "docs/reference/runtime.md", "# Runtime\n\n" + paragraph + "\n\n## 源码入口\n[code](../../src/runtime.py)\n")
    result = checker.check(repo)
    assert result["violations"] == []
    assert {x["rule"] for x in result["review_hints"]} == {"long-document", "possible-duplication"}


def test_two_hop_routes_cover_development_tasks():
    checker = load_checker()
    root = Path(__file__).resolve().parents[2]
    document = checker.parse("docs/README.md", (root / "docs/README.md").read_text())
    targets = {checker.local_target(root, document.name, link.target)[0] for link in document.links}
    assert {
        "docs/guides/development.md", "docs/reference/tools.md", "docs/reference/configuration.md",
        "docs/reference/runtime.md", "docs/reference/context.md", "docs/reference/evaluation.md",
        "docs/reference/harness.md", "docs/reference/console.md",
    } <= targets
