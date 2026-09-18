import subprocess
import copy
import pytest
from chatcopilot.core.source_snapshot import source_manifest, copy_sources
from chatcopilot.core.source_manifest import is_deployable_source_path
from chatcopilot.harness.verification_ledger import SourceLedger
from chatcopilot.harness.verification_policy import policy_path
from chatcopilot.harness.repository_checks import RepositoryChecks, compare_verification
from chatcopilot.harness.models import HarnessError

def fixture(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, text in {".gitignore": "ignored/\n.env\n", ".env.example": "PUBLIC=example\n",
        "src/demo.py": "value = 1\n", "scripts/check_repo.py": "raise SystemExit(1)\n",
        "tests/test_demo.py": "def test_true():\n    assert True\n", "pyproject.toml": ""}.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    manifest = source_manifest(root)
    frozen = tmp_path / "frozen"
    copy_sources(root, frozen, manifest)
    ledger = SourceLedger(frozen, manifest, tmp_path / "inventory", root)
    return root, frozen, ledger

@pytest.mark.parametrize("name", [".env", ".env.local", "local.env", "bots/demo/local.env", ".env/private.example", ".env/env.example"])
def test_private_environment_is_excluded(name):
    assert not is_deployable_source_path(name)

@pytest.mark.parametrize("name", [".env.example", "env.example", "deploy/wsl/console.env.example", "bots/demo/local.env.example"])
def test_public_environment_template_is_included(name):
    assert is_deployable_source_path(name)

@pytest.mark.parametrize("name", ["src/chatcopilot/evals/suites/project-business-v1/cases.yaml",
    "src/chatcopilot/evals/business_policy.py", "console/web/src/features/harness/repairV2.test.ts"])
def test_existing_expectations_and_frontend_tests_remain_fixed(name):
    assert policy_path(name)

def test_passing_exit_cannot_hide_changed_tests_or_new_skips():
    from chatcopilot.harness.repository_checks import compare_verification
    baseline = {'passed': True, 'checks': [{'name': 'core tests', 'exit_code': 0,
        'test_inventory': {'sha256': 'fixed-tests', 'count': 2, 'skipped_ids': []}}]}
    candidate = copy.deepcopy(baseline)
    assert compare_verification(baseline, candidate) == []
    candidate['checks'][0]['test_inventory']['skipped_ids'] = ['test_missing']
    assert compare_verification(baseline, candidate) is None
    candidate['checks'][0]['test_inventory'] = {'sha256': 'fewer-tests', 'count': 1, 'skipped_ids': []}
    assert compare_verification(baseline, candidate) is None
    candidate['checks'][0].pop('test_inventory')
    assert compare_verification(baseline, candidate) is None


def test_fixed_inventory_rejects_new_ignore_authority(tmp_path):
    root, frozen, ledger = fixture(tmp_path)
    (root / 'src/.gitignore').write_text('*.py\n')
    with pytest.raises(HarnessError, match='候选新增'):
        ledger.manifest(root)


def test_candidate_checker_cannot_replace_trusted_check_entry(tmp_path):
    root, frozen, ledger = fixture(tmp_path)
    (root / 'scripts/check_repo.py').write_text('raise SystemExit(0)\n')
    checks = RepositoryChecks(tmp_path / 'checks', root)
    checks.bind(ledger, frozen)
    checks.view(root, tmp_path / 'view')
    checks.view(root, tmp_path / 'candidate', checkers=False)
    assert (tmp_path / 'view/scripts/check_repo.py').read_text() == 'raise SystemExit(1)\n'
    assert (tmp_path / 'candidate/scripts/check_repo.py').read_text() == 'raise SystemExit(0)\n'


def test_unknown_static_failure_is_not_accepted_as_existing_debt():
    before = {'passed': False, 'checks': [{'name': 'Ruff', 'exit_code': 1}]}
    assert compare_verification(before, copy.deepcopy(before)) is None
