from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generated_requirements_are_in_sync() -> None:
    sync_requirements = _load_script("sync_requirements.py")
    assert sync_requirements.check() == []


def test_validation_profiles_include_static_and_runtime_checks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    check_repo = _load_script("check_repo.py")
    profiles = check_repo._profiles()
    fast_names = [check.name for check in profiles["fast"]]
    full_names = [check.name for check in profiles["full"]]
    assert fast_names == [
        "SDD metadata",
        "public repository boundary",
        "architecture boundaries",
        "requirements drift",
        "UTF-8 source normalization",
        "Ruff",
        "typed contracts",
        "core tests",
    ]
    assert full_names[-4:] == [
        "installed dependency consistency",
        "Python wheel build smoke",
        "full Python tests",
        "console production build",
    ]
    fast_pytest = profiles["fast"][-1]
    full_pytest = profiles["full"][-2]
    assert f"--basetemp={tmp_path / 'chatcopilot-pytest-fast'}" in fast_pytest.argv
    assert f"--basetemp={tmp_path / 'chatcopilot-pytest-full'}" in full_pytest.argv


def test_validation_subprocesses_use_one_wsl_temp_root(
    monkeypatch,
    tmp_path: Path,
) -> None:
    check_repo = _load_script("check_repo.py")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("TEMP", "/mnt/c/Temp/example")
    monkeypatch.setenv("TMP", "/mnt/c/Temp/example")

    env = check_repo._check_env()

    assert env["TMPDIR"] == str(tmp_path)
    assert env["TEMP"] == str(tmp_path)
    assert env["TMP"] == str(tmp_path)


def test_build_smoke_detects_tracked_file_changes(tmp_path: Path) -> None:
    build_smoke = _load_script("build_smoke.py")
    tracked = tmp_path / "tracked.py"
    before = {tracked: None}
    after = {tracked: "changed"}
    assert build_smoke._changed_paths(before, after) == (tracked,)


def test_gitleaks_wrapper_enforces_private_three_scope_scans() -> None:
    script = (ROOT / "scripts" / "check_secrets.sh").read_text(encoding="utf-8")

    assert "umask 077" in script
    assert "checkout-index --all" in script
    assert '"$candidate_root/index"' in script
    assert '"$candidate_root/worktree"' in script
    assert '"$candidate_root/untracked"' in script
    assert "--modified" in script
    assert "--others --exclude-standard" in script
    assert '--gitleaks-ignore-path "$EMPTY_IGNORE"' in script
    assert "--ignore-gitleaks-allow" in script
    assert "--max-decode-depth=2" in script
    assert "--report-format=json" in script
    assert '--report-path "$report"' in script
    assert "> /dev/null 2>&1" in script
    assert "unsupported candidate path type: %s" not in script
    assert 'printf \'%s\\n\' "$relative_path"' not in script


def test_gitleaks_policy_covers_network_and_query_leaks() -> None:
    config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")

    assert 'id = "agentstrata-private-host"' in config
    assert 'id = "agentstrata-private-ipv4"' in config
    assert 'id = "agentstrata-sensitive-uri-query"' in config
    assert 'description = "Standard WSL localhost bridge hostname"' in config
    assert 'regexTarget = "match"' in config
    assert r"wsl\.localhost" in config
    for marker in (
        ".corp",
        ".home",
        ".internal",
        ".intranet",
        ".lan",
        ".local",
        "access_token",
        "client_secret",
        "192.168.",
    ):
        assert marker in config

    def rule_regex(rule_id: str) -> re.Pattern[str]:
        rule = config.split(f'id = "{rule_id}"', maxsplit=1)[1]
        rule = rule.split("[[rules]]", maxsplit=1)[0]
        match = re.search(r"regex = '''(.+?)'''", rule, flags=re.DOTALL)
        assert match is not None
        return re.compile(match.group(1))

    private_host = rule_regex("agentstrata-private-host")
    private_suffix = ".".join(("private", "lan"))
    assert private_host.search(f"https://service.{private_suffix}/artifact")
    assert private_host.search("service." + "internal")
    assert private_host.search("127.example." + "local" + "/path")
    assert not private_host.search(".env.local")

    sensitive_query = rule_regex("agentstrata-sensitive-uri-query")
    api_key = "api" + "_key"
    token = "to" + "ken"
    fixture_value = "abcdefghijkl"
    assert sensitive_query.search(
        f"https://api.example.com/data?{api_key}={fixture_value}"
    )
    assert sensitive_query.search(
        f"https://api.example.com/data?page=1&{token}={fixture_value}"
    )
    assert not sensitive_query.search("api_key=fallback.api_key")


def test_release_runbook_preserves_signed_tag_and_draft_boundaries() -> None:
    runbook = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")

    assert "0.1.0.dev0" in runbook
    assert "git tag -s v0.1.0" in runbook
    assert "git push origin refs/tags/v0.1.0" in runbook
    assert "scripts/check_public_repo.py --history" in runbook
    assert "scripts/check_secrets.sh history" in runbook
    assert "draft GitHub Release" in runbook
    assert "不发布 PyPI" in runbook
