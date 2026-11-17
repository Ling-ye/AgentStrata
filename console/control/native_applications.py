"""Only connection locators are stored here; Windows owns application data."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess

from chatcopilot.core.install_artifacts import write_private
from chatcopilot.core.private_sqlite import private_file


def exchange(request: dict) -> dict:
    script = Path(__file__).resolve().parents[2] / "studio/windows/management.ps1"
    from studio.delivery import windows_path
    command = ["/init", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-NonInteractive",
               "-STA", "-ExecutionPolicy", "Bypass", "-File", windows_path(script)]
    try:
        result = subprocess.run(command, input=json.dumps(request, ensure_ascii=False), capture_output=True,
                                text=True, encoding="utf-8", timeout=180 if request.get("select") else 45)
        if result.returncode:
            raise ConnectionError("桌宠连接暂不可用，请确认客户端后台在线且已支持工作台接入。")
        value = json.loads(result.stdout.lstrip("\ufeff"))
        if value.get("error"):
            if value.get("code") == "settings_conflict":
                from chatcopilot.core.application_settings import SettingsConflict
                raise SettingsConflict("配置已被更新，请重新读取后保存。")
            raise ValueError("原生应用操作未完成")
        return value
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        raise ConnectionError("无法连接 Windows 桌宠，请确认客户端后台在线。") from exc


class NativeApplications:
    def __init__(self, home: Path):
        self.root = home / "native-connections"

    def _path(self, ident):
        if not re.fullmatch(r"native:[a-f0-9]{32}", ident):
            raise ValueError("原生应用标识无效")
        return self.root / (ident[7:] + ".json")

    def records(self):
        if not self.root.exists():
            return []
        return [self.record("native:" + path.stem) for path in sorted(self.root.glob("*.json"))]

    def record(self, ident):
        path = self._path(ident)
        private_file(path)
        value = json.loads(path.read_text())
        if value.get("id") != ident:
            raise ValueError("原生应用连接标识不匹配")
        return value

    def connect(self):
        value = exchange({"select": True, "operation": "identity"})
        if value.get("canceled"):
            return value
        identity = value["result"]
        ident = "native:" + identity["id"]
        record = {"id": ident, "connection_file": value["connection_file"], "name": identity["name"], "type": identity.get("type", "desktop")}
        write_private(self._path(ident), json.dumps(record, ensure_ascii=False))
        return {"id": ident}

    def read(self, ident, operation, arguments=None):
        record = self.record(ident)
        return exchange({"connection_file": record["connection_file"], "expected_id": ident[7:],
                         "operation": operation, "arguments": arguments or {}})["result"]

    def write(self, ident, section, expected, values):
        record = self.record(ident)
        intent = {"section": section, "revision": expected, "values": values}
        key = hashlib.sha256(json.dumps(intent, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return exchange({"connection_file": record["connection_file"], "expected_id": ident[7:],
                         "write": intent, "request_id": key})["result"]
