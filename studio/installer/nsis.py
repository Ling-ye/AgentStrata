"""Studio's locked/fallback NSIS compiler selection."""
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[2]


def compile(stage):
    if shutil.which("makensis"):
        command = ["makensis", "-V2", "setup.nsi"]
    elif shutil.which("node") and (ROOT / "integrations/live2d_desktop/node_modules/app-builder-lib").is_dir():
        command = ["node", str(ROOT / "integrations/desktop_app/nsis.cjs")]
    else:
        raise ValueError("Windows 安装包构建端需要 NSIS 3，或 integrations/live2d_desktop 的锁定 Node 构建工具")
    subprocess.run(command, cwd=stage, check=True, timeout=900)
