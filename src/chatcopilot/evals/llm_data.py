"""Prepare complete pinned LLM datasets and publish verified cache receipts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from typing import Any
import zipfile

from chatcopilot.evals.bfcl_official import CATEGORY_COUNTS, RELEVANCE, REVISION as BFCL_REVISION, filename, rows_to_cases
from chatcopilot.evals.ifeval_official import REVISION as IFEVAL_REVISION, build_checker
from chatcopilot.evals.ifeval_resources import REVISION as NLTK_REVISION, configure_resources, resource_dir

BFCL_BASE = f"https://raw.githubusercontent.com/ShishirPatil/gorilla/{BFCL_REVISION}/berkeley-function-call-leaderboard/bfcl_eval/data"
IFEVAL_URL = f"https://raw.githubusercontent.com/google-research/google-research/{IFEVAL_REVISION}/instruction_following_eval/data/input_data.jsonl"
BFCL_FILES = tuple(filename(c) for c in CATEGORY_COUNTS) + tuple("possible_answer/" + filename(c) for c in CATEGORY_COUNTS if c not in RELEVANCE)


def root() -> Path:
    return Path(os.environ.get("CHATCOPILOT_EVALS_DATA_DIR") or "~/.cache/agentstrata/evals").expanduser()


def bfcl_dir() -> Path:
    return root() / "bfcl" / "official" / BFCL_REVISION


def ifeval_path() -> Path:
    return root() / "ifeval" / "official" / IFEVAL_REVISION / "input_data.jsonl"


def verified(directory: Path, revision: str, files: tuple[str, ...]) -> bool:
    try:
        receipt = json.loads((directory / "source.json").read_text(encoding="utf-8"))
        hashes = receipt.get("files")
        return (receipt.get("revision") == revision and isinstance(hashes, dict)
                and set(hashes) == set(files)
                and all((directory / name).is_file() and not (directory / name).is_symlink()
                        and hashlib.sha256((directory / name).read_bytes()).hexdigest() == hashes[name] for name in files))
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def download(url: str) -> bytes:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    with requests.Session() as client:
        client.mount("https://", HTTPAdapter(max_retries=Retry(total=6, backoff_factor=.5, status_forcelist=(429, 500, 502, 503, 504))))
        response = client.get(url, timeout=120)
        response.raise_for_status()
        return response.content


def publish(staging: Path, destination: Path, revision: str, files: tuple[str, ...], count: int, **extra) -> dict[str, Any]:
    receipt = {"revision": revision, "case_count": count,
               "files": {name: hashlib.sha256((staging / name).read_bytes()).hexdigest() for name in files}, **extra}
    destination.mkdir(parents=True, exist_ok=True)
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        (staging / name).replace(target)
    pending = destination / "source.json.tmp"
    pending.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(destination / "source.json")
    return {"ready": True, **receipt}


def prepare_bfcl() -> dict[str, Any]:
    destination = bfcl_dir()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not verified(destination, BFCL_REVISION, BFCL_FILES):
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix="prepare-") as temp:
            staging = Path(temp)

            def fetch(name):
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(download(BFCL_BASE + "/" + name))

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(fetch, BFCL_FILES))
            cases = rows_to_cases(staging, strict_counts=True)
            publish(staging, destination, BFCL_REVISION, BFCL_FILES, len(cases), categories=CATEGORY_COUNTS,
                    source="BFCL V4 official single-turn", scope="single_turn")
    return {"ready": True, "path": str(destination), "case_count": sum(CATEGORY_COUNTS.values()), "revision": BFCL_REVISION}


def prepare_nltk() -> None:
    destination = resource_dir()
    expected = ("tokenizers/punkt/english.pickle", "tokenizers/punkt_tab/english/abbrev_types.txt",
                "tokenizers/punkt_tab/english/collocations.tab", "tokenizers/punkt_tab/english/ortho_context.tab",
                "tokenizers/punkt_tab/english/sent_starters.txt")
    if not verified(destination, NLTK_REVISION, expected):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix="prepare-") as temp:
            staging = Path(temp)
            for package in ("punkt", "punkt_tab"):
                payload = download(f"https://raw.githubusercontent.com/nltk/nltk_data/{NLTK_REVISION}/packages/tokenizers/{package}.zip")
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    for name in expected:
                        member = name.removeprefix("tokenizers/")
                        if member.startswith(package + "/"):
                            target = staging / name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(archive.read(member))
            publish(staging, destination, NLTK_REVISION, expected, 0, source="NLTK punkt English resources")
    configure_resources()
    from chatcopilot.evals.vendor.ifeval.instructions_util import _get_sentence_tokenizer
    _get_sentence_tokenizer.cache_clear()
    _get_sentence_tokenizer()


def prepare_ifeval() -> dict[str, Any]:
    prepare_nltk()
    destination = ifeval_path().parent
    files = ("input_data.jsonl",)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not verified(destination, IFEVAL_REVISION, files):
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix="prepare-") as temp:
            staging = Path(temp)
            path = staging / files[0]
            path.write_bytes(download(IFEVAL_URL))
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            if len(rows) != 541 or len({row["key"] for row in rows}) != 541:
                raise ValueError("IFEval fixed official data must contain 541 unique prompts")
            for row in rows:
                if not row.get("prompt") or len(row["instruction_id_list"]) != len(row["kwargs"]):
                    raise ValueError("IFEval prompt/constraint data is incomplete")
                for ident, parameters in zip(row["instruction_id_list"], row["kwargs"]):
                    build_checker(ident, parameters, row["prompt"])
            publish(staging, destination, IFEVAL_REVISION, files, len(rows), source="Google Research IFEval")
    return {"ready": True, "path": str(ifeval_path()), "case_count": 541, "revision": IFEVAL_REVISION}
