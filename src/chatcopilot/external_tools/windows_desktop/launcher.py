"""Fixed Windows-MCP stdio launcher; model credentials never cross into Windows."""
from __future__ import annotations

import argparse
import base64
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys


LINUX_ENV = frozenset({"WSL_INTEROP", "WSL_DISTRO_NAME", "HOME", "USER", "LOGNAME", "PATH",
                       "SHELL", "XDG_RUNTIME_DIR", "LANG", "LC_ALL"})
WINDOWS_ENV = ("SystemRoot", "SystemDrive", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "USERPROFILE",
               "USERNAME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "ProgramFiles", "ProgramData",
               "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


def _quote(value: str) -> str:
    if any(ord(character) < 32 for character in value):
        raise ValueError("Windows launch paths cannot contain control characters")
    return "'" + value.replace("'", "''") + "'"


def command(python: str, script: str) -> list[str]:
    if not PureWindowsPath(python).is_absolute() or not python.lower().endswith(".exe"):
        raise ValueError("Configure an absolute native Windows Python executable")
    keep = ",".join(_quote(value) for value in WINDOWS_ENV)
    code = ("$ErrorActionPreference='Stop'; "
            "[Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false); "
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
            f"$keep=@({keep}); Get-ChildItem Env: | Where-Object {{$_.Name -notin $keep}} | Remove-Item; "
            "$env:ANONYMIZED_TELEMETRY='false'; $env:PYTHONUTF8='1'; "
            f"& {_quote(python)} '-u' {_quote(script)}; exit $LASTEXITCODE")
    encoded = base64.b64encode(code.encode("utf-16-le")).decode()
    return ["/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile",
            "-NonInteractive", "-EncodedCommand", encoded]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True)
    args = parser.parse_args(argv)
    script = Path(__file__).with_name("native_server.py").resolve()
    environment = {key: value for key, value in os.environ.items() if key in LINUX_ENV}
    native = subprocess.run(["wslpath", "-w", str(script)], env=environment, capture_output=True,
                            text=True, check=True, timeout=10).stdout.strip()
    # Inherit stdio unchanged: MCP owns the stream and closes it during shutdown.
    return subprocess.run(command(args.python, native), env=environment, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Windows desktop MCP could not start: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1)
