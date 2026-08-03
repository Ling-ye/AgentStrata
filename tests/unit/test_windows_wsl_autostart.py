from __future__ import annotations

from pathlib import Path


def test_windows_wsl_autostart_is_user_scoped_and_reversible() -> None:
    script = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "wsl"
        / "win"
        / "install-wsl-autostart.ps1"
    ).read_text(encoding="utf-8")

    assert "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Run" in script
    assert "-WindowStyle Hidden" in script
    assert "--exec /bin/true" in script
    assert "New-ItemProperty" in script
    assert "Remove-ItemProperty -LiteralPath $RunKey" in script
    assert '$Property = $Properties.PSObject.Properties[$RunValueName]' in script
    assert '& "`$env:SystemRoot\\System32\\wsl.exe"' in script
    assert "[switch]$Probe" in script
    assert "wsl-autostart-status.json" in script
    assert "Register-ScheduledTask" not in script
    assert "-RunLevel Highest" not in script
