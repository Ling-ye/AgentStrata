"""Installed plugin assets; independent of an AgentStrata source checkout."""
from importlib.resources import files
import json


def resource(name: str):
    return files(__package__).joinpath("resources", name)


def manifest() -> dict:
    return json.loads(resource("upstream.json").read_text())


def artifact_lock() -> dict:
    return json.loads(resource("artifacts.json").read_text())
