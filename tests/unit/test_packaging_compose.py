"""Compose packaging resolves all services before preparing Docker images."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from chatcopilot.deployment.packaging.containers import prepare


HEALTHCHECK = {"test": ["CMD", "/probe"]}
PINNED_IMAGE = "example.invalid/worker@sha256:" + "a" * 64


@pytest.fixture
def compose_package(tmp_path):
    source = tmp_path / "source"
    stage = tmp_path / "stage"
    payload = stage / "payload"
    payload.mkdir(parents=True)

    def package(services, spec, docker_runner):
        files = {
            "compose.yaml": yaml.safe_dump({"services": services}, sort_keys=False).encode(),
            "install.json": json.dumps(spec).encode(),
            "Dockerfile": b"FROM scratch\n",
            "jobs/Containerfile": b"FROM scratch\n",
            "notice.txt": b"Redistribution notice\n",
        }
        manifest = {}
        for name, content in files.items():
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            manifest[name] = {"sha256": hashlib.sha256(content).hexdigest(), "executable": False}
        return prepare(source, manifest, "example", stage, payload, "revision", spec, prefix="",
                       docker_runner=docker_runner)

    return package, stage, payload


@pytest.mark.parametrize(
    ("worker", "message"),
    [
        ({"build": {"context": "jobs", "dockerfile": "missing"}}, "缺少本地 Dockerfile"),
        ({"image": PINNED_IMAGE}, "提供再分发许可"),
    ],
)
def test_invalid_later_service_does_not_call_docker(compose_package, worker, message):
    package, _, payload = compose_package
    services = {
        "web": {"build": ".", "healthcheck": HEALTHCHECK},
        "worker": {**worker, "healthcheck": HEALTHCHECK},
    }

    def unexpected_docker(*args, **kwargs):
        pytest.fail(f"Docker ran before validating each service: {args}")

    with pytest.raises(ValueError, match=message):
        package(services, {}, unexpected_docker)

    assert list(payload.iterdir()) == []


def test_prepares_short_and_long_builds_and_fixed_image(compose_package):
    package, stage, payload = compose_package
    services = {
        "web": {"build": ".", "healthcheck": HEALTHCHECK},
        "jobs": {"build": {"context": "jobs", "dockerfile": "Containerfile"}, "healthcheck": HEALTHCHECK},
        "worker": {"image": PINNED_IMAGE, "healthcheck": HEALTHCHECK},
    }
    spec = {"entry": {"service": "web", "port": 8080}, "image_licenses": {"worker": "notice.txt"}}
    image_ids = {name: "sha256:" + digit * 64 for name, digit in zip(services, "123", strict=True)}
    tags = {name: f"agentstrata/example-{name}:revision-{digest[7:19]}" for name, digest in image_ids.items()}
    calls = []

    def record_docker(*args, cwd=None):
        calls.append((args, cwd))
        if args[0] == "build":
            iid = Path(args[4])
            iid.write_text(image_ids[iid.stem] + "\n")
        if args[:2] == ("image", "inspect"):
            return image_ids["worker"]
        if args[:2] == ("image", "save"):
            Path(args[3]).touch()
        return ""

    config = package(services, spec, record_docker)

    assert calls == [
        (("build", "--platform", "linux/amd64", "--iidfile", str(stage / "web.iid"),
          "--file", "Dockerfile", "."), stage / "context"),
        (("tag", image_ids["web"], tags["web"]), None),
        (("build", "--platform", "linux/amd64", "--iidfile", str(stage / "jobs.iid"),
          "--file", "Containerfile", "."), stage / "context" / "jobs"),
        (("tag", image_ids["jobs"], tags["jobs"]), None),
        (("pull", "--platform", "linux/amd64", PINNED_IMAGE), None),
        (("image", "inspect", "--format", "{{.Id}}", PINNED_IMAGE), None),
        (("tag", image_ids["worker"], tags["worker"]), None),
        (("image", "save", "-o", str(payload / "images.tar"), *tags.values()), None),
    ]
    assert config["images"] == {tags[name]: image_ids[name] for name in services}
    assert (payload / "images.txt").read_text() == "".join(f"{tag} {digest}\n" for tag, digest in config["images"].items())
    assert (payload / "images.tar").is_file()
    assert (payload / "LICENSE-worker.txt").read_bytes() == b"Redistribution notice\n"
    assert (payload / "entry-port").read_text() == "web 8080\n"
    assert (payload / "source" / "jobs" / "Containerfile").is_file()
    compose = yaml.safe_load((payload / "compose.yaml").read_text())
    assert compose["services"] == {
        name: {
            "image": tag, "platform": "linux/amd64", "pull_policy": "never",
            "restart": "unless-stopped", "healthcheck": HEALTHCHECK,
            **({"ports": ["127.0.0.1::8080"]} if name == "web" else {}),
        }
        for name, tag in tags.items()
    }
