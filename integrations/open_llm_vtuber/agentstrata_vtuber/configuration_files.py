"""Private environment files and persistent upstream data links."""
import json
from pathlib import Path
from chatcopilot.core.install_artifacts import InstallError, private_directory, write_private


def environment_file(path: Path, values: dict[str, str]) -> None:
    write_private(path, "\n".join(key + "=" + json.dumps(str(value), ensure_ascii=False)
                                 for key, value in values.items()) + "\n")


def persistent_link(path: Path, target: Path) -> None:
    private_directory(target)
    if path.is_symlink():
        if path.resolve() != target.resolve():
            raise InstallError("Unexpected persistent-data link")
        return
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise InstallError("Refusing to replace existing upstream data")
        path.rmdir()
    path.symlink_to(target, target_is_directory=True)
