"""Real PowerShell 5.1 process pipes and argument round trips; no WSL setup."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell process boundary")

DRIVER = r'''
param([string]$Source,[string]$CaseFile)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Production launcher has syntax errors'}
$functions=@($ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('New-StudioWslProcessInfo','Invoke-StudioWsl')},$true))
if($functions.Count -ne 2){throw 'Production WSL process functions not found'}
foreach($function in $functions){. ([ScriptBlock]::Create($function.Extent.Text))}
$case=ConvertFrom-Json ([IO.File]::ReadAllText($CaseFile,[Text.Encoding]::UTF8))
$Wsl=[string]$case.executable
try {
  $accepted=@(0);if($null -ne $case.success_exit_codes){$accepted=[int[]]$case.success_exit_codes}
  $output=Invoke-StudioWsl -Values ([string[]]$case.values) -FailureMessage 'test bridge failed' -SuccessExitCodes $accepted -PassThru:([bool]$case.pass_thru)
  $report=[ordered]@{ok=$true;output=$output;preference=[string]$ErrorActionPreference;owner=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value}
} catch {$report=[ordered]@{ok=$false;error=$_.Exception.Message;preference=[string]$ErrorActionPreference}}
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$report|ConvertTo-Json -Compress
'''

CHILD = r'''
import json,os,sys
from pathlib import Path
mode=sys.argv[1]
if mode=='echo':
    print('nonfatal notice: \u63d0\u793a',file=sys.stderr)
    print(json.dumps({'values':sys.argv[2:],'utf8':os.environ.get('WSL_UTF8')},ensure_ascii=False))
elif mode=='flood':
    sys.stdout.write('o'*262144)
    sys.stderr.write('e'*262144)
elif mode=='fail':
    print('http://127.0.0.1:1/#login=not-success')
    sys.stderr.write('old detail '*1000+'FINAL_FAILURE')
    sys.exit(7)
elif mode=='marker':
    Path(sys.argv[2]).write_text('unexpected process started')
elif mode=='restart':
    print('restart requested')
    print('nonfatal setup notice',file=sys.stderr)
    sys.exit(3010)
elif mode=='identity':
    import csv,subprocess
    text=subprocess.check_output([str(Path(os.environ['SystemRoot'])/'System32/whoami.exe'),'/user','/fo','csv','/nh']).decode('utf-8',errors='replace')
    print(json.dumps({'sid':next(csv.reader(text.splitlines()))[-1]}))
'''


@pytest.fixture
def invoke(tmp_path):
    driver = tmp_path / "driver.ps1"
    child = tmp_path / "child.py"
    driver.write_text(DRIVER, encoding="utf-8")
    child.write_text(CHILD, encoding="utf-8")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"

    def call(mode, *values, success_exit_codes=None, pass_thru=False):
        case = tmp_path / "case.json"
        case.write_text(json.dumps({"executable": sys.executable,
            "values": ["-B", str(child), mode, *values], "success_exit_codes": success_exit_codes,
            "pass_thru": pass_thru}), encoding="utf-8")
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(driver),
            "-Source", str(ROOT / "studio/windows/start.ps1"), "-CaseFile", str(case)],
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, capture_output=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        return json.loads(result.stdout.decode("utf-8-sig"))

    return call


@pytest.mark.parametrize("value", ["plain", "", "path with spaces", "中文目录", "quote'path",
    "literal$(printf marker)end", "literal`printf marker`end", "C:\\path with spaces\\"])
def test_literal_arguments_and_successful_stderr(invoke, value):
    result = invoke("echo", value)
    assert result["ok"] is True
    assert result["preference"] == "Stop"
    assert json.loads(result["output"]) == {"values": [value], "utf8": "1"}


def test_large_stdout_and_stderr_are_drained_without_deadlock(invoke):
    result = invoke("flood")
    assert result["ok"] is True
    assert result["output"] == "o" * 262144


def test_nonzero_exit_is_not_hidden_by_stdout_and_failure_is_bounded(invoke):
    result = invoke("fail")
    assert result["ok"] is False
    assert "test bridge failed (exit 7)" in result["error"]
    assert result["error"].endswith("FINAL_FAILURE")
    assert len(result["error"]) < 1800
    assert result["preference"] == "Stop"


def test_installer_restart_exit_is_explicitly_accepted_and_returned(invoke):
    result = invoke("restart", success_exit_codes=[0, 3010], pass_thru=True)
    assert result["ok"] is True
    assert result["output"] == {"Output": "restart requested\r\n", "ExitCode": 3010}


def test_wsl_process_keeps_the_windows_callers_identity(invoke):
    result = invoke("identity")
    assert result["ok"] is True
    assert json.loads(result["output"])["sid"] == result["owner"]


@pytest.mark.parametrize("value", ['bad"quote', "bad\nline", "bad\rline", "bad\x00null"])
def test_invalid_argument_is_rejected_before_start(invoke, tmp_path, value):
    marker = tmp_path / "must-not-exist"
    result = invoke("marker", str(marker), value)
    assert result["ok"] is False
    assert "Invalid local WSL argument" in result["error"]
    assert not marker.exists()


PROVISION_DRIVER = r'''
param([string]$Source,[string]$Scenario)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Production launcher has syntax errors'}
$functions=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Initialize-StudioDistro'},$true))
if($functions.Count -ne 1){throw 'Production distro preparation function missing'}
. ([ScriptBlock]::Create($functions[0].Extent.Text))
$Distro='Ubuntu-24.04';$script:Installed=$false
$script:Trace=[Collections.Generic.List[object]]::new()
function Progress([string]$Text){}
function Start-Process {throw 'No elevation or secondary process launcher is allowed'}
function New-ItemProperty {return $null}
function Invoke-StudioWsl([string[]]$Values,[string]$FailureMessage,[int[]]$SuccessExitCodes=@(0),[switch]$PassThru){
  $script:Trace.Add([pscustomobject]@{values=$Values;accepted=$SuccessExitCodes;pass_thru=[bool]$PassThru})
  if($Values[0] -eq '--list'){
    if($Scenario -eq 'restart' -and $script:Installed){throw 'WSL enumeration is unavailable until restart'}
    if($Scenario -eq 'present' -or ($script:Installed -and $Scenario -ne 'pending')){return "Ubuntu-22.04`nUbuntu-24.04`0`n"}
    return "Ubuntu-22.04`n"
  }
  if($Values[0] -ne '--install'){throw 'Unexpected WSL command'}
  if($Scenario -eq 'error'){throw ($FailureMessage+' (exit 5). fixture refusal')}
  $script:Installed=$true
  $code=0;if($Scenario -eq 'restart'){$code=3010}
  return [pscustomobject]@{ExitCode=$code;Output='fixture install; not executed'}
}
try{$ready=Initialize-StudioDistro -ResumeLauncher ''; $report=@{ok=$true;ready=$ready;trace=$script:Trace.ToArray()}}
catch{$report=@{ok=$false;error=$_.Exception.Message;trace=$script:Trace.ToArray()}}
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$report|ConvertTo-Json -Compress -Depth 6
'''


@pytest.mark.parametrize("scenario", ["present", "new", "restart", "pending", "error"])
def test_first_preparation_preserves_user_and_never_borrows_an_administrator(tmp_path, scenario):
    driver = tmp_path / "provision.ps1"
    driver.write_text(PROVISION_DRIVER, encoding="utf-8")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {key: value for key, value in os.environ.items() if key.upper() not in {"PSMODULEPATH", "PSMODULEANALYSISCACHEPATH"}}
    process = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(driver),
        "-Source", str(ROOT / "studio/windows/start.ps1"), "-Scenario", scenario],
        env=env, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode == 0, process.stderr.decode("utf-8", errors="replace")
    report = json.loads(process.stdout.decode("utf-8-sig"))
    installs = [row for row in report["trace"] if row["values"][0] == "--install"]
    assert len(installs) == (0 if scenario == "present" else 1)
    if installs:
        assert installs[0]["values"] == ["--install", "--no-launch", "-d", "Ubuntu-24.04"]
        assert installs[0]["accepted"] == [0, 3010] and installs[0]["pass_thru"] is True
    if scenario == "error":
        assert report["ok"] is False and "current Windows user" in report["error"]
    else:
        assert report["ok"] is True
        assert report["ready"] is (scenario in {"present", "new"})


@pytest.mark.parametrize("mode", ["failed", "missing", "success"])
def test_packager_artifact_contract(tmp_path, mode):
    node = shutil.which("node.exe") or str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs/node.exe")
    if not Path(node).is_file():
        pytest.skip("Existing Node required; this test never installs tooling")
    version = subprocess.check_output([node, "--version"], creationflags=subprocess.CREATE_NO_WINDOW).decode().strip()
    major, minor = (int(p) for p in version.lstrip("v").split(".")[:2])
    if major < 22 or major == 22 and minor < 12:
        pytest.skip("Existing Node 22.12+ required; this test never installs tooling")
    recipe = tmp_path / "recipe/integrations/desktop_app"
    recipe.mkdir(parents=True)
    shutil.copyfile(ROOT / "integrations/desktop_app/build.ps1", recipe / "build.ps1")
    live = recipe.parent / "live2d_desktop"
    live.mkdir()
    lock = b'{"name":"local-test-only"}'
    (live / "package-lock.json").write_bytes(lock)
    (live / "package.json").write_text("{}", encoding="utf-8")
    builder = tmp_path / "existing-tools"
    tools = builder / hashlib.sha256(lock).hexdigest().upper()
    (tools / "node_modules/electron/dist").mkdir(parents=True)
    (tools / "ready").touch()
    (tools / "node_modules/electron/dist/electron.exe").write_bytes(b"inert test marker; never executed")
    # Only this controlled JS stub runs; no Electron or NSIS is invoked.
    fake = "console.log('OUT_DIAGNOSTIC');console.error('ERR_DIAGNOSTIC');"
    if mode == "failed":
        fake += "process.exitCode=7;"
    elif mode == "success":
        fake += "const fs=require('node:fs'),p=require('node:path');const d=p.join(process.argv[3],'release');fs.mkdirSync(d);fs.writeFileSync(p.join(d,'application.exe'),'inert successful test output');"
    (recipe / "package.cjs").write_text(fake, encoding="utf-8")
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "framework.tar.gz").write_bytes(b"fixture archive is not opened by this build stub")
    (stage / "framework.sha256").write_text("a" * 64, encoding="utf-8")
    (stage / "desktop-build.json").write_text("{}", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k.upper() not in {"PSMODULEPATH", "PSMODULEANALYSISCACHEPATH"}}
    env["PATH"] = str(Path(node).parent) + os.pathsep + env.get("PATH", "")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(recipe / "build.ps1"),
        "-Studio", "-Stage", str(stage), "-BuildRoot", str(builder)], env=env,
        capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    if mode == "success":
        assert result.returncode == 0, result.stderr
        assert (stage / "application.exe").read_bytes() == b"inert successful test output"
    else:
        assert result.returncode != 0
        output = result.stdout + result.stderr
        assert b"OUT_DIAGNOSTIC" in output and b"ERR_DIAGNOSTIC" in output
        assert not (stage / "application.exe").exists()
