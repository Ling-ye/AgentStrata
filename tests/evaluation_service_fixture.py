"""Real service entrypoint with only operator-local env input isolated for tests."""
from pathlib import Path
from chatcopilot.evals.application import bots


def main():
    original = bots.load_local_env_values
    bundled = Path(__file__).resolve().parents[1] / "bots"
    def values(path, **kwargs):
        if Path(path).resolve().is_relative_to(bundled) and Path(path).name == "local.env":
            return {}
        return original(path, **kwargs)
    bots.load_local_env_values = values
    from chatcopilot.evals.service.__main__ import main as serve
    return serve()


if __name__ == "__main__":
    raise SystemExit(main())
