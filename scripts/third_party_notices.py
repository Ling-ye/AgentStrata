#!/usr/bin/env python3
"""Refresh pinned dependency license evidence, or check it offline.

Refresh downloads archives without installing or executing package code. The
archive hashes come from the existing lockfiles. Legal interpretation and copied
repository assets remain reviewed in THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zipfile

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "third_party/dependencies.json"
NOTICES = ROOT / "THIRD_PARTY_LICENSES.txt"
LICENSE_NAME = re.compile(r"^(licen[sc]es?|copying|notices?|copyright|authors|third[-_ ]party(?:[-_ ](?:licen[sc]es?|notices?))?)(?:[._-]|$)", re.I)


def _is_license_file(name: str) -> bool:
    base = PurePosixPath(name).name
    # Components such as Arco's notice.js implement UI notifications; their
    # source code is not a legal NOTICE file.
    return bool(LICENSE_NAME.match(base)) and PurePosixPath(base).suffix.lower() not in {
        ".js", ".mjs", ".cjs", ".ts", ".py", ".map", ".c", ".h", ".cpp", ".go",
    }


def _read_url(url: str) -> bytes:
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "AgentStrata-license-evidence"}), timeout=90) as response:
                return response.read()
        except URLError as error:
            if isinstance(error, HTTPError) and error.code < 500 and error.code != 429:
                raise
            if attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))
    raise AssertionError("unreachable")


def dependencies() -> list[dict]:
    packages = tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    scopes: dict[str, set[str]] = {}
    by_name = {p["name"]: p for p in packages}
    project = by_name["agentstrata"]
    groups = {"runtime": project.get("dependencies", [])}
    groups.update(project.get("optional-dependencies", {}))
    for group, roots in groups.items():
        pending = [p["name"] for p in roots]
        visited: set[str] = set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            scopes.setdefault(name, set()).add(group)
            pending.extend(d["name"] for d in by_name.get(name, {}).get("dependencies", []))
    build = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)",
                           (ROOT / "requirements/release-build.txt").read_text(), re.M))
    result = []
    for p in packages:
        if p["name"] == "agentstrata":
            continue
        candidates = p.get("wheels", []) + ([p["sdist"]] if "sdist" in p else [])
        artifact = min(candidates, key=lambda a: a.get("size", float("inf")))
        source = p.get("sdist", artifact)
        roles = scopes.get(p["name"], set()).copy()
        if build.get(p["name"]) == p["version"]:
            roles.add("release-build")
        result.append({"ecosystem": "python", "name": p["name"], "version": p["version"],
                       "scopes": sorted(roles), "artifact_url": artifact["url"],
                       "integrity": artifact["hash"], "source_url": source["url"],
                       "source_integrity": source["hash"]})
    for lock_path, scope in [("console/web/package-lock.json", "console")]:
        lock = json.loads((ROOT / lock_path).read_text())
        npm_scopes: dict[str, set[str]] = {}
        roots = lock["packages"][""]
        for group, names in (("runtime", roots.get("dependencies", {})),
                             ("development", roots.get("devDependencies", {}))):
            for name in names:
                role = ("build" if name in {"vite", "@vitejs/plugin-react", "typescript"}
                        or name.startswith("@types/") else "test" if group == "development" else group)
                pending = ["node_modules/" + name]
                visited: set[str] = set()
                while pending:
                    key = pending.pop()
                    if key in visited or key not in lock["packages"]:
                        continue
                    visited.add(key)
                    npm_scopes.setdefault(key, set()).add(role)
                    package = lock["packages"][key]
                    if package.get("link"):
                        package = lock["packages"][package["resolved"]]
                    for dependency in {**package.get("dependencies", {}), **package.get("optionalDependencies", {})}:
                        parent = key
                        while True:
                            candidate = (parent + "/" if parent else "") + "node_modules/" + dependency
                            if candidate in lock["packages"]:
                                pending.append(candidate)
                                break
                            if not parent:
                                break
                            parent = parent.rpartition("node_modules/")[0].rstrip("/")
        for path, p in lock["packages"].items():
            if not path or p.get("link") or "node_modules/" not in path:
                continue
            result.append({"ecosystem": "npm", "name": path.split("node_modules/")[-1],
                           "version": p["version"], "scopes": [scope, *sorted(npm_scopes.get(path, {"development" if p.get("dev") else "runtime-or-build"}))],
                           "artifact_url": p["resolved"], "integrity": p["integrity"],
                           "source_url": p["resolved"], "source_integrity": p["integrity"],
                           "lockfile": lock_path, "package_path": path,
                           "optional": bool(p.get("optional")), "license": p.get("license", "")})
    return sorted(result, key=lambda p: (p["ecosystem"], p["name"], p["version"], p.get("package_path", "")))


def _download(url: str, integrity: str) -> bytes:
    cache = Path(tempfile.gettempdir()) / "agentstrata-license-archives"
    cache.mkdir(exist_ok=True)
    target = cache / hashlib.sha256(url.encode()).hexdigest()
    if target.exists():
        data = target.read_bytes()
    else:
        data = _read_url(url)
    if ":" in integrity:
        algorithm, expected = integrity.split(":", 1)
        actual = hashlib.new(algorithm, data).hexdigest()
    else:
        algorithm, expected = integrity.split("-", 1)
        actual = base64.b64encode(hashlib.new(algorithm, data).digest()).decode()
    if actual != expected:
        raise ValueError(f"archive integrity mismatch: {url}")
    if not target.exists():
        target.write_bytes(data)
    return data


def _members(data: bytes, url: str, notice_files: tuple[str, ...] = ()) -> dict[str, bytes]:
    def wanted(name: str) -> bool:
        base = PurePosixPath(name).name
        return name in notice_files or _is_license_file(name) or base in {"METADATA", "PKG-INFO", "package.json"}
    if url.endswith(".whl") or zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return {name: archive.read(name) for name in archive.namelist() if wanted(name) and not name.endswith("/")}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        result = {}
        for member in archive:
            if member.isfile() and wanted(member.name):
                stream = archive.extractfile(member)
                if stream is not None:
                    result[member.name] = stream.read()
        return result


def inspect_package(p: dict) -> tuple[dict, str]:
    reviewed = json.loads((ROOT / "third_party/license_sources.json").read_text()).get(
        f"{p['ecosystem']}:{p['name']}:{p['version']}", {})
    members = _members(_download(p["artifact_url"], p["integrity"]), p["artifact_url"],
                       tuple(reviewed.get("copyright_notice_files", [])))
    license_bytes = {name: value for name, value in sorted(members.items())
                     if _is_license_file(name)}
    licenses = {name: value.decode("utf-8") for name, value in license_bytes.items()}
    p = p.copy()
    for name in reviewed.get("copyright_notice_files", []):
        source = members[name]
        comments = [match.group() for match in re.finditer(rb"/\*.*?\*/", source, re.S)
                    if re.search(rb"copyright|@license|SPDX-License-Identifier", match.group(), re.I)]
        if not comments:
            raise ValueError(f"reviewed source has no copyright/license headers: {name}")
        label = f"{name} (original copyright/license comment excerpts)"
        license_bytes[label] = b"\n\n".join(comments)
        licenses[label] = license_bytes[label].decode("utf-8")
        p.setdefault("notice_sources", {})[name] = hashlib.sha256(source).hexdigest()
    if p["ecosystem"] == "python":
        meta = next((value for name, value in members.items() if name.endswith("/METADATA")), None)
        if meta is None:
            meta = next(value for name, value in members.items() if name.endswith("/PKG-INFO"))
        metadata = BytesParser().parsebytes(meta)
        p["license"] = metadata.get("License-Expression") or metadata.get("License") or "; ".join(
            v.removeprefix("License :: ") for v in metadata.get_all("Classifier", []) if v.startswith("License ::"))
        p["upstream_urls"] = [str(value) for value in metadata.get_all("Project-URL", [])]
        p["upstream_authors"] = {key: str(metadata.get(key, "")) for key in ("Author", "Author-email")}
    else:
        package_path = min((name for name in members if name.endswith("/package.json")), key=len)
        package = json.loads(members[package_path])
        metadata_url = f"https://registry.npmjs.org/{p['name']}/{p['version']}"
        p["upstream_metadata_url"] = metadata_url
        published = json.loads(_read_url(metadata_url)) if "runtime" in p["scopes"] else {}
        p["upstream_repository"] = package.get("repository", "")
        p["upstream_repository"] = p["upstream_repository"] or published.get("repository", "")
        p["upstream_authors"] = package.get("author", package.get("contributors", ""))
        repository = p["upstream_repository"]
        repository_url = repository.get("url", "") if isinstance(repository, dict) else repository
        repository_url = repository_url.rstrip("/")
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository_url):
            repository_url = "https://github.com/" + repository_url
        match = re.search(r"github.com[/:]([^/]+/[^/#]+?)(?:\.git)?$", repository_url)
        revision = published.get("gitHead", "")
        if match and re.fullmatch(r"[a-f0-9]{40}", revision):
            p["upstream_revision"] = revision
            p["preferred_source_url"] = f"https://github.com/{match[1]}/archive/{revision}.tar.gz"
        if not licenses and not reviewed:
            published = published or json.loads(_read_url(metadata_url))
            repository = p["upstream_repository"] or published.get("repository", {})
            repository_url = repository.get("url", "") if isinstance(repository, dict) else repository
            match = re.search(r"github.com[/:]([^/]+/[^/#]+?)(?:\.git)?$", repository_url)
            if match:
                tags = ["v" + p["version"], p["version"], p["name"] + "@" + p["version"]]
                if re.fullmatch(r"[a-f0-9]{40}", published.get("gitHead", "")):
                    tags.insert(0, published["gitHead"])
                for suffix in (f"{tag}/{name}" for tag in tags
                               for name in ("LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE-MIT")):
                    url = f"https://raw.githubusercontent.com/{match[1]}/{suffix}"
                    try:
                        license_bytes[url] = _read_url(url)
                        licenses[url] = license_bytes[url].decode()
                        p["license_evidence_url"] = url
                        break
                    except HTTPError as exc:
                        if exc.code != 404:
                            raise
    if reviewed:
        p["reviewed_evidence"] = reviewed
        for key in ("preferred_source_url", "upstream_revision", "selected_license"):
            if reviewed.get(key):
                p[key] = reviewed[key]
        for url in reviewed.get("license_urls", []):
            license_bytes[url] = _read_url(url)
            licenses[url] = license_bytes[url].decode()
        if reviewed.get("declaration_only"):
            p["evidence_status"] = "declaration only; original license text not supplied"
            license_bytes[package_path] = members[package_path]
            licenses["Upstream package.json declaration (not an original license file)"] = json.dumps(
                {key: package.get(key) for key in ("name", "version", "license", "author", "repository")}, indent=2)
            reference = "third_party/MIT-REFERENCE-LICENSE.txt"
            license_bytes[reference] = (ROOT / reference).read_bytes()
            licenses["Standard MIT reference, not an upstream copyright notice"] = license_bytes[reference].decode()
    # Keep exact upstream declarations; SPDX classification is not guessed from a name.
    p["license"] = str(p["license"] or "See package license text")
    if not licenses:
        raise ValueError(f"no license evidence in {p['ecosystem']} {p['name']} {p['version']}")
    p["license_files"] = {name: hashlib.sha256(data).hexdigest() for name, data in license_bytes.items()}
    body = "\n\n".join(f"--- {name} ---\n{text}" for name, text in licenses.items())
    section = (f"\n{'=' * 78}\n{p['ecosystem']}: {p['name']} {p['version']}\n"
               f"Scopes: {', '.join(p['scopes'])}\nDeclared license: {p['license']}\n"
               f"Evidence: {p.get('evidence_status', 'original package or pinned upstream license text')}\n"
               f"Source/archive: {p['source_url']}\nIntegrity: {p['source_integrity']}\n"
               + (f"Preferred upstream source/build materials: {p['preferred_source_url']}\n" if p.get("preferred_source_url") else "")
               + f"\n{body}\n")
    return p, section


def check() -> None:
    index = json.loads(INDEX.read_text())
    expected = dependencies()
    actual = index["dependencies"]
    for records in (expected, actual):
        records.sort(key=lambda p: (p["ecosystem"], p["name"], p["version"], p.get("package_path", "")))
    if len(expected) != len(actual) or any(any(a.get(k) != b.get(k) for k in a) for a, b in zip(expected, actual)):
        raise ValueError("dependency license inventory differs from lockfiles; refresh and review notices")
    if hashlib.sha256(NOTICES.read_bytes()).hexdigest() != index["notices_sha256"]:
        raise ValueError("third-party license text does not match the reviewed inventory")
    if hashlib.sha256((ROOT / "third_party/license_sources.json").read_bytes()).hexdigest() != index["reviewed_sources_sha256"]:
        raise ValueError("reviewed license sources changed; refresh and review evidence")
    for record in actual:
        if not record.get("license_files"):
            raise ValueError(f"missing license evidence: {record['name']}")
        if record.get("reviewed_evidence", {}).get("review_status") == "pending-maintainer-decision":
            raise ValueError(f"license material gap awaits maintainer decision: {record['name']}")
    print(f"Third-party license evidence matches {len(actual)} locked dependency records.")


def main() -> None:
    global ROOT, INDEX, NOTICES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="check an isolated source candidate")
    parser.add_argument("--refresh", action="store_true", help="download hash-locked archives and regenerate evidence; review before release")
    args = parser.parse_args()
    if args.root:
        ROOT = args.root.resolve()
        INDEX = ROOT / "third_party/dependencies.json"
        NOTICES = ROOT / "THIRD_PARTY_LICENSES.txt"
    if not args.refresh:
        check()
        return
    def attempt(package):
        try:
            return inspect_package(package)
        except Exception as error:
            print(f"UNRESOLVED {package['ecosystem']} {package['name']} {package['version']}: {error}", flush=True)
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, dependencies()))
    inspected = [result for result in results if result is not None]
    if len(inspected) != len(results):
        raise ValueError(f"{len(results) - len(inspected)} dependencies lack complete license evidence; no inventory written")
    notice = ("Third-party dependency license and copyright texts\n"
              "Generated from hash-verified locked archives by scripts/third_party_notices.py.\n"
              "Scopes identify available dependencies, not a claim that every component is redistributed.\n"
              "See THIRD_PARTY_NOTICES.md for copied assets, license choices and integration boundaries.\n"
              + "".join(section for _, section in inspected))
    NOTICES.write_text(notice, encoding="utf-8")
    INDEX.parent.mkdir(exist_ok=True)
    INDEX.write_text(json.dumps({"notices_sha256": hashlib.sha256(notice.encode()).hexdigest(),
                                "reviewed_sources_sha256": hashlib.sha256((ROOT / "third_party/license_sources.json").read_bytes()).hexdigest(),
                                "dependencies": [p for p, _ in inspected]}, indent=2, ensure_ascii=False) + "\n")
    check()


if __name__ == "__main__":
    main()
