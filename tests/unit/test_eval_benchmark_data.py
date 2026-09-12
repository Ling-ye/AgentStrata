"""Official preparation contracts with local Parquet; no Hub or model calls."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chatcopilot.evals import benchmark_data as data
from chatcopilot.evals.adapters import gaia, swebench, swebench_runtime
from chatcopilot.evals.application.catalog import list_case_summaries
from chatcopilot.evals.official_data import prepare_official_data, suite_data_status


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("CHATCOPILOT_EVALS_DATA_DIR", str(tmp_path / "cache"))
    for key in ("CHATCOPILOT_SWEBENCH_DATA_PATH", "CHATCOPILOT_GAIA_DATA_PATH", "CHATCOPILOT_GAIA_SMOKE"):
        monkeypatch.delenv(key, raising=False)


def test_swe_pinned_download_converts_all_rows_and_auto_discovers(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import huggingface_hub

    rows = [dict(instance_id=f"sample__repo-{i}", problem_statement="Fix sample", image="sample/image:one",
                 eval_script="echo tests", base_commit="a" * 40, repo="sample/repo", version="1",
                 FAIL_TO_PASS=["test_bug"], PASS_TO_PASS=[], log_parser="parse_log_django", eval_type="pass_and_fail")
            for i in range(500)]
    parquet = tmp_path / "test.parquet"
    pq.write_table(pa.Table.from_pylist(rows), parquet)
    calls = []

    def download(repo, name, **kwargs):
        calls.append((repo, name, kwargs))
        return str(parquet)

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    result = prepare_official_data("swe-bench-verified")
    assert result["case_count"] == 500
    assert calls[0][0] == data.SWE_REPO
    assert calls[0][2]["revision"] == data.SWE_REVISION
    assert len(swebench.load_cases()) == 500
    assert suite_data_status("swe-bench-verified")["source"] == "official_cache"
    data.prepare_swe_data()
    assert len(calls) == 1
    Path(result["path"]).write_text("damaged")
    assert data.swe_data_path() is None


def test_gaia_requires_auth_and_never_silently_uses_ungated_mirrors(monkeypatch, tmp_path):
    monkeypatch.setattr(data, "hf_token", lambda: None)
    with pytest.raises(ValueError, match="hf auth login"):
        data.prepare_gaia_data(tmp_path)
    assert not (tmp_path / data.GAIA_REVISION / "source.json").exists()


def test_gaia_snapshot_and_attachments_are_complete_before_cache_is_visible(monkeypatch, tmp_path):
    import pyarrow as pa
    import pyarrow.parquet as pq
    import huggingface_hub

    monkeypatch.setattr(data, "hf_token", lambda: "test-credential")
    monkeypatch.setenv("CHATCOPILOT_GAIA_MAX_CASES", "1")

    def download(repo, **kwargs):
        assert repo == data.GAIA_REPO and kwargs["revision"] == data.GAIA_REVISION
        assert kwargs["allow_patterns"] == ["2023/validation/*", "README.md"]
        split = Path(kwargs["local_dir"]) / "2023" / "validation"
        split.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([{"task_id": "sample", "Question": "Read the file",
            "Final answer": "sample", "Level": 1, "file_name": "sample.txt"}]), split / "metadata.parquet")
        (split / "sample.txt").write_text("sample")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", download)
    result = gaia.prepare_data()
    assert result["case_count"] == 1
    assert len(gaia.load_cases(auto_download=False)) == 1
    attachment = Path(result["path"]).parent / "sample.txt"
    attachment.unlink()
    assert gaia.find_cached_data() == ""
    assert gaia.load_cases(auto_download=False) == ()


def test_swe_catalog_lists_unprepared_cases_and_checks_images_in_one_read(tmp_path, monkeypatch):
    rows = [{"instance_id": f"sample__repo-{i}", "problem_statement": "fix", "image": f"sample/image:{i}"} for i in range(2)]
    path = tmp_path / "data.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    monkeypatch.setenv("CHATCOPILOT_SWEBENCH_DATA_PATH", str(path))
    calls = []
    monkeypatch.setattr(swebench_runtime.shutil, "which", lambda _: "/usr/bin/docker")

    def docker(args, **kwargs):
        calls.append(args)
        return 0, json.dumps({"Repository": "sample/image", "Tag": "0", "Digest": "<none>"})

    monkeypatch.setattr(swebench_runtime, "docker", docker)
    summaries = list_case_summaries("swe-bench-verified")
    assert len(summaries) == 2
    assert summaries[0]["readiness"]["ready"] is True
    assert summaries[1]["readiness"]["ready"] is False
    assert "sample/image:1" in summaries[1]["readiness"]["reason"]
    assert len(calls) == 1 and calls[0][:2] == ["image", "ls"]


def test_repository_preparation_only_restores_disposable_container(monkeypatch):
    calls = []
    head = iter(["b" * 40, "a" * 40])

    def docker(args, **kwargs):
        calls.append(args)
        return 0, next(head) if args[-2:] == ["rev-parse", "HEAD"] else ""

    monkeypatch.setattr(swebench_runtime, "docker", docker)
    with pytest.raises(ValueError, match="managed"):
        swebench_runtime.prepare_repository("production", "a" * 40)
    assert not calls
    result = swebench_runtime.prepare_repository("agentstrata-swe-" + "c" * 32 + "-solve", "a" * 40)
    assert result == {"image_head": "b" * 40, "base_commit": "a" * 40}
    assert all(args[:3] == ["exec", "--workdir", "/testbed"] for args in calls)
    assert calls[2][-3:] == ["reset", "--hard", "a" * 40]


def test_corrupt_receipt_is_not_a_ready_dataset(tmp_path):
    folder = tmp_path / data.GAIA_REVISION
    folder.mkdir()
    (folder / "source.json").write_text("[]")
    assert data.prepared_path(tmp_path, data.GAIA_REVISION) is None
