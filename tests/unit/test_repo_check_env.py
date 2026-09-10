import importlib.util
from pathlib import Path
import sys


def test_temporary_git_objects_are_scoped_to_repository_checks(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('repository_check_env', Path(__file__).parents[2] / 'scripts/check_repo.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    keys = ('GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES')
    for key in keys:
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    assert all(key not in module._check_env(uses_repository_index=False) for key in keys)
    assert all(module._check_env(uses_repository_index=True)[key] == str(tmp_path / key.lower()) for key in keys)
