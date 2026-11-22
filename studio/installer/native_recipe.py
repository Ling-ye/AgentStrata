"""Studio-owned native Windows payload preparation."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from studio import delivery


ROOT = Path(__file__).resolve().parents[2]


def build(source, dest, *, bot, kind, name, origin, release=None, assets=None, install_id=None, windows_build=None):
    identity = install_id or bot + "-" + origin["revision"][:10]
    with tempfile.TemporaryDirectory(prefix="studio-native-assets-") as temporary:
        if kind == "companion" and assets is None:
            from chatcopilot.deployment.windows_dependencies import compiler_path
            assets = Path(temporary) / "payload"
            preparation = subprocess.run([
                sys.executable, str(ROOT / "integrations/live2d_desktop/scripts/prepare-runtime.py"),
                "--source", str(source), "--output", str(assets),
                "--build-root", str(Path(temporary) / "work"), "--uv", str(compiler_path()),
            ], stderr=subprocess.PIPE, text=True, timeout=1800)
            if preparation.returncode:
                if "runtime-inputs" in preparation.stderr or "MCP source policy" in preparation.stderr:
                    raise ValueError("构建端缺少桌宠锁定的 Windows-MCP 原始输入。请准备源码、Python 归档及完整 wheel 缓存，或导入与所选版本匹配的 Windows 运行载荷。")
                if "patched project sources are incomplete" in preparation.stderr:
                    raise ValueError("桌宠缺少当前锁文件对应的插件 wheel、配套源码或上游项目归档。")
                if "lack runtime admission" in preparation.stderr:
                    raise ValueError("桌宠插件的原始输入已核对，Windows 前端资源、运行环境与安装身份仍需独立验收。请导入已核验的匹配载荷。")
                raise ValueError("桌宠 Windows 资源准备失败。请核对原生构建输入，或在「接入已有应用」导入匹配的已核验载荷。")
        return delivery.pack(source, dest, bot_relative=f"bots/{bot}/bot.yaml", release=release,
                             assets=assets, name=name, install_id=identity,
                             windows_build=windows_build or os.environ.get("AGENTSTRATA_STUDIO_WINDOWS_BUILD"))
