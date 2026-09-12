"""Pinned official data preparation, separate from model and container execution."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

GAIA_REPO = "gaia-benchmark/GAIA"
GAIA_REVISION = "682dd723ee1e1697e00360edccf2366dc8418dd9"
SWE_REPO = "SWE-bench/SWE-bench_Verified"
SWE_REVISION = "78f471bf655a3137b2e8a75af1501690ec009ec3"


def cache_root() -> Path:
    return Path(os.environ.get("CHATCOPILOT_EVALS_DATA_DIR") or "~/.cache/agentstrata/evals").expanduser()


def hf_token() -> str | None:
    from huggingface_hub import get_token

    return os.environ.get("CHATCOPILOT_HF_TOKEN", "").strip() or get_token()


def prepared_path(root: Path, revision: str) -> Path | None:
    folder = root / revision
    receipt = folder / "source.json"
    if not receipt.is_file():
        return None
    try:
        source = json.loads(receipt.read_text(encoding="utf-8"))
        if not isinstance(source, dict) or not isinstance(source.get("attachments", {}), dict):
            return None
        relative = source["jsonl"]
        path = folder / relative
        if path.resolve().is_relative_to(folder.resolve()) and source["revision"] == revision:
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == source["jsonl_sha256"]:
                for name, digest in source.get("attachments", {}).items():
                    attachment = path.parent / name
                    if not attachment.resolve().is_relative_to(path.parent.resolve()) or not attachment.is_file():
                        return None
                    if hashlib.sha256(attachment.read_bytes()).hexdigest() != digest:
                        return None
                return path
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


def swe_data_path() -> Path | None:
    return prepared_path(cache_root() / "swe-bench-verified" / "official", SWE_REVISION)


def _publish(root: Path, revision: str, repo: str, relative: str,
             rows: list[dict[str, Any]], *, attachments: dict[str, str] | None = None) -> dict[str, Any]:
    folder = root / revision
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".jsonl.tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    temp.replace(path)
    source = {"repo_id": repo, "revision": revision, "case_count": len(rows), "jsonl": relative,
              "jsonl_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "attachments": attachments or {}}
    receipt = folder / "source.json.tmp"
    receipt.write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8")
    receipt.replace(folder / "source.json")
    return {"ready": True, "path": str(path), **source}


def prepare_swe_data() -> dict[str, Any]:
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq

    root = cache_root() / "swe-bench-verified" / "official"
    path = swe_data_path()
    if path:
        return {"ready": True, "path": str(path), "revision": SWE_REVISION}
    parquet = hf_hub_download(SWE_REPO, "data/test-00000-of-00001.parquet", repo_type="dataset",
                              revision=SWE_REVISION, local_dir=root / SWE_REVISION, token=False)
    rows = pq.read_table(parquet).to_pylist()
    required = {"instance_id", "problem_statement", "image", "eval_script", "base_commit",
                "repo", "version", "FAIL_TO_PASS", "PASS_TO_PASS", "log_parser", "eval_type"}
    if (len(rows) != 500 or any(not required.issubset(row) for row in rows)
            or len({row.get("instance_id") for row in rows}) != 500):
        raise ValueError("SWE-bench 官方数据数量或执行字段与固定版本不符")
    return _publish(root, SWE_REVISION, SWE_REPO, "instances.jsonl", rows)


def prepare_gaia_data(root: Path) -> dict[str, Any]:
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import GatedRepoError
    import pyarrow.parquet as pq

    path = prepared_path(root, GAIA_REVISION)
    if path:
        return {"ready": True, "path": str(path), "revision": GAIA_REVISION}
    token = hf_token()
    if not token:
        raise ValueError("GAIA 官方数据需要先在 Hugging Face 同意访问条件并执行 hf auth login；也可配置 CHATCOPILOT_HF_TOKEN。不要将 Token 输入测评题目。")
    folder = root / GAIA_REVISION
    try:
        snapshot_download(GAIA_REPO, repo_type="dataset", revision=GAIA_REVISION,
                          local_dir=folder, token=token, allow_patterns=["2023/validation/*", "README.md"])
    except GatedRepoError as exc:
        raise ValueError("当前 Hugging Face 账号尚未获得 GAIA 数据访问权限；请先在官方数据页同意访问条件。") from exc
    split = folder / "2023" / "validation"
    rows = pq.read_table(split / "metadata.parquet").to_pylist()
    if not rows or any(not row.get("Question") or not row.get("Final answer") for row in rows):
        raise ValueError("GAIA validation 数据缺少题目或参考答案")
    attachments = {}
    for row in rows:
        name = row.get("file_name", "")
        if not name:
            continue
        file = split / name
        if Path(name).name != name or not file.is_file() or file.is_symlink():
            raise ValueError("GAIA 附件缺失或路径不合法")
        attachments[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    return _publish(root, GAIA_REVISION, GAIA_REPO, "2023/validation/metadata.jsonl", rows,
                    attachments=attachments)
