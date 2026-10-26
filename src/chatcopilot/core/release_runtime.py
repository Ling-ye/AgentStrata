"""Launch an installed service from its selected immutable source version."""
from __future__ import annotations

import argparse
from importlib.util import find_spec
import json
import os
from pathlib import Path
import re
import sys
import uuid

from chatcopilot.contracts.model_runtime import digest
from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_directory
from chatcopilot.core.release_identity import (
    configuration_manifest, environment_identity, execution_identity, process_identity, read_json, tree_manifest,
)

CONTEXT = "AGENTSTRATA_RELEASE_CONTEXT"


def validate_selection(control: Path, selected: dict) -> Path:
    from chatcopilot.core.source_snapshot import manifest_digest, verify_copy
    source = Path(selected["source_root"])
    if (not re.fullmatch(r"[a-f0-9]{64}", selected["version"])
            or source != control / "versions" / selected["version"] / "source" or source.resolve() != source):
        raise ValueError("Selected source is outside its managed version")
    manifest = read_json(source.parent / "manifest.json")
    if manifest_digest(manifest) != selected["version"]:
        raise ValueError("Selected source manifest does not match its version")
    verify_copy(source, manifest)
    if tree_manifest(source) != {name: row["sha256"] for name, row in manifest.items()}:
        raise ValueError("Selected source contains unrecorded files")
    return source


def loaded(module_file: str, *, state_root: Path | None = None) -> None:
    """Publish after the actual service module has initialized, never from model text."""
    reference = os.environ.get(CONTEXT)
    if not reference:
        return
    path = Path(reference)
    context = read_json(path)
    observed = Path(module_file).resolve()
    if not any(observed.is_relative_to(Path(root)) for root in context["component_roots"].values()):
        raise ValueError("Loaded service module is outside its selected release")
    components = {name: digest(tree_manifest(Path(root))) for name, root in context["component_roots"].items()}
    if components != context["components"] or process_identity(os.getpid()) != context["process"]:
        raise ValueError("Service release changed during startup")
    if context.get("bot"):
        bot = Path(context["bot"])
        facts = execution_identity(bot, dict(os.environ))
        if (digest(configuration_manifest(bot)) != context["configuration"] or facts["model"] != context["model"]
                or facts["environment"] != context["environment"]):
            raise ValueError("Application configuration changed during startup")
    receipt = {**context, "loaded": True, "module": str(observed), "admission_protocol": 1,
               "state_root": str(state_root) if state_root else None}
    write_private(path.parent / (context["service"] + ".json"), json.dumps(receipt, sort_keys=True))
    os.environ.pop(CONTEXT, None)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--component", action="append", required=True)
    parser.add_argument("--bot", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.service) or any(
            not re.fullmatch(r"[a-z][a-z0-9_]*", name) for name in args.component):
        raise ValueError("Invalid managed service identity")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or Path(command[0]).resolve() != Path(sys.executable).resolve():
        raise ValueError("Managed release must retain the installed Python interpreter")
    control = private_directory(args.control)
    running = private_directory(control / "running")
    pointer = control / "current.json"
    selected = read_json(pointer) if pointer.exists() else None
    if selected:
        validate_selection(control, selected)
    roots = {}
    for name in args.component:
        if selected:
            relative = selected["component_paths"][name]
            root = Path(selected["source_root"]) / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("Release component escaped the selected source")
        else:
            spec = find_spec(name)
            if spec is None or spec.origin is None:
                raise ValueError("Installed service component is unavailable: " + name)
            root = Path(spec.origin).resolve().parent
        roots[name] = str(root)
    bot = args.bot
    if selected and bot:
        if "--bot" not in command or command[command.index("--bot") + 1] != str(bot):
            raise ValueError("Managed Gateway command does not identify its configured application")
        bot = Path(selected["source_root"]) / selected["bot_relative"]
        command[command.index("--bot") + 1] = str(bot)
    environment = dict(os.environ)
    environment["AGENTSTRATA_RELEASE_CONTROL"] = str(control)
    if selected:
        prefixes = list(dict.fromkeys(str(Path(root).parent) for root in roots.values()))
        environment["PYTHONPATH"] = os.pathsep.join([*prefixes, *([environment["PYTHONPATH"]] if environment.get("PYTHONPATH") else [])])
    nonce = uuid.uuid4().hex
    # A fresh cache location prevents timestamp-based bytecode reuse after a switch.
    environment["PYTHONPYCACHEPREFIX"] = str(control / "cache" / nonce)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if bot:
        environment["CHATCOPILOT_BOT_SPEC"] = str(bot)
        from chatcopilot.core.application_configuration import load_application_spec
        gateway = load_application_spec(bot).raw.get("gateway", {})
        gate_root = environment.get(gateway.get("state_root_env", "CHATCOPILOT_GATEWAY_STATE_ROOT"))
    else:
        gate_root = environment.get("CHATCOPILOT_GATEWAY_STATE_ROOT")
    if gate_root:
        gate_root = str(Path(gate_root).expanduser().absolute())
        environment["AGENTSTRATA_RELEASE_GATE_ROOT"] = gate_root
    components = {name: digest(tree_manifest(Path(root))) for name, root in roots.items()}
    model = execution_identity(bot, environment) if bot else {"model": None, "environment": environment_identity()}
    identity = {"service": args.service, "process": process_identity(os.getpid()), "launch_id": nonce,
        "version": selected["version"] if selected else None, "component_roots": roots, "components": components,
        "environment": model["environment"], "command": digest(command), "loaded": False,
        "bot": str(bot) if bot else None, "configuration": digest(configuration_manifest(bot)) if bot else None,
        "model": model["model"]}
    identity["gate_root"] = gate_root
    if selected:
        expected = selected["services"][args.service]
        for key in ("components", "environment", "configuration", "model", "gate_root"):
            if identity[key] != expected[key]:
                raise ValueError("Selected release no longer matches its prepared " + key)
    context = running / (args.service + ".launch.json")
    write_private(context, json.dumps(identity, sort_keys=True))
    environment[CONTEXT] = str(context)
    os.execve(command[0], command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
