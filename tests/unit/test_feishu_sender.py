"""单元测试：agent.feishu_sender 的路径白名单 / 大小校验 / subprocess 调用。

运行：
    python -m pytest tests/unit/test_feishu_sender.py

仓库目前没有统一的 pytest 设置，这里用 stdlib unittest 跑关键分支：
- 工作区内：results / downloads / attachments / uploads 路径放行
- 越权：工作区外路径拒绝
- 不存在：FileNotFoundError
- 不限制单次文件数量或单文件大小
- subprocess 失败：RuntimeError

cc-connect 二进制调用全程 mock，不依赖真实环境。
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from chatcopilot.platforms.feishu import sender as feishu_sender
from chatcopilot.middleware.runtime.workspace import Workspace


def _make_workspace(tmp_root: Path) -> Workspace:
    """构造一个临时 Workspace，并保证子目录都存在。"""
    ws = Workspace(root=tmp_root, chat_kind="p2p", chat_id="test", user_id="u1")
    return ws.ensure()


class ResolveSendablePathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name).resolve()
        self.ws = _make_workspace(self.tmp_root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _touch(self, relative_to_root: str, *, size: int = 0) -> Path:
        target = (self.ws.root / relative_to_root).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as fp:
            if size:
                fp.seek(size - 1)
                fp.write(b"\0")
        return target

    def test_results_path_is_accepted(self) -> None:
        f = self._touch("results/diff.xlsx", size=1024)
        resolved = feishu_sender.resolve_sendable_paths(self.ws, [str(f)])
        self.assertEqual(resolved, [f])

    def test_downloads_relative_path_is_accepted(self) -> None:
        f = self._touch("downloads/sheet.csv", size=128)
        resolved = feishu_sender.resolve_sendable_paths(self.ws, ["downloads/sheet.csv"])
        self.assertEqual(resolved, [f])

    def test_bare_filename_matches_unique_workspace_file(self) -> None:
        f = self._touch("results/diff.xlsx", size=128)
        resolved = feishu_sender.resolve_sendable_paths(self.ws, ["diff.xlsx"])
        self.assertEqual(resolved, [f])

    def test_bare_filename_conflict_requires_relative_path(self) -> None:
        self._touch("results/report.xlsx", size=128)
        self._touch("downloads/report.xlsx", size=128)
        with self.assertRaises(ValueError) as ctx:
            feishu_sender.resolve_sendable_paths(self.ws, ["report.xlsx"])
        self.assertIn("不唯一", str(ctx.exception))
        self.assertIn("results", str(ctx.exception))
        self.assertIn("downloads", str(ctx.exception))

    def test_attachments_path_is_accepted(self) -> None:
        f = self._touch(".cc-connect/attachments/raw.csv", size=64)
        resolved = feishu_sender.resolve_sendable_paths(self.ws, [str(f)])
        self.assertEqual(resolved, [f])

    def test_uploads_path_is_accepted(self) -> None:
        f = self._touch("uploads/x.csv", size=64)
        resolved = feishu_sender.resolve_sendable_paths(self.ws, [str(f)])
        self.assertEqual(resolved, [f])

    def test_workspace_root_file_is_accepted(self) -> None:
        f = self._touch("MEMORY.md", size=64)
        resolved = feishu_sender.resolve_sendable_paths(self.ws, ["MEMORY.md"])
        self.assertEqual(resolved, [f])

    def test_outside_workspace_path_is_rejected(self) -> None:
        outside = self.tmp_root.parent / "outside.txt"
        outside.write_bytes(b"x")
        with self.assertRaises(PermissionError):
            feishu_sender.resolve_sendable_paths(self.ws, [str(outside)])

    def test_missing_file_raises_not_found(self) -> None:
        ghost = self.ws.results / "no_such.xlsx"
        with self.assertRaises(FileNotFoundError):
            feishu_sender.resolve_sendable_paths(self.ws, [str(ghost)])

    def test_large_file_is_accepted(self) -> None:
        f = self._touch("results/big.bin", size=60 * 1024 * 1024)
        resolved = feishu_sender.resolve_sendable_paths(self.ws, [str(f)])
        self.assertEqual(resolved, [f])

    def test_empty_files_list_raises(self) -> None:
        with self.assertRaises(ValueError):
            feishu_sender.resolve_sendable_paths(self.ws, [])

    def test_many_files_are_accepted(self) -> None:
        paths = [
            str(self._touch(f"results/r{i}.txt", size=8))
            for i in range(8)
        ]
        resolved = feishu_sender.resolve_sendable_paths(self.ws, paths)
        self.assertEqual([str(p) for p in resolved], paths)

    def test_dedup_keeps_order(self) -> None:
        a = self._touch("results/a.xlsx", size=16)
        b = self._touch("results/b.xlsx", size=16)
        resolved = feishu_sender.resolve_sendable_paths(
            self.ws, [str(a), str(b), str(a)]
        )
        self.assertEqual(resolved, [a, b])


class SendViaCcConnectTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name).resolve()
        self.ws = _make_workspace(self.tmp_root)
        self.sample = self.ws.results / "diff.xlsx"
        self.sample.write_bytes(b"x" * 32)
        # 模拟一个 cc-connect 二进制（仅文件存在即可，subprocess 会被 mock）
        self.bin_dir = self.tmp_root / "bin"
        self.bin_dir.mkdir()
        self.cc_bin = self.bin_dir / "cc-connect"
        self.cc_bin.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
        os.chmod(self.cc_bin, 0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _patch_env(self) -> "mock._patch":
        return mock.patch.dict(
            os.environ,
            {"CHATCOPILOT_CC_CONNECT_BIN": str(self.cc_bin)},
            clear=False,
        )

    def test_success_passes_arguments(self) -> None:
        with self._patch_env(), mock.patch(
            "chatcopilot.platforms.feishu.sender.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="sent", stderr=""
            )
            out = feishu_sender.send_via_cc_connect([self.sample], message="report")
        self.assertEqual(out, "sent")
        run.assert_called_once()
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[0], str(self.cc_bin))
        self.assertEqual(cmd[1], "send")
        self.assertIn("--file", cmd)
        self.assertIn(str(self.sample), cmd)
        self.assertIn("--message", cmd)
        self.assertIn("report", cmd)

    def test_nonzero_exit_raises_runtime(self) -> None:
        with self._patch_env(), mock.patch(
            "chatcopilot.platforms.feishu.sender.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=2, stdout="", stderr="no active session"
            )
            with self.assertRaises(RuntimeError) as ctx:
                feishu_sender.send_via_cc_connect([self.sample])
        self.assertIn("no active session", str(ctx.exception))

    def test_timeout_raises_timeout_error(self) -> None:
        with self._patch_env(), mock.patch(
            "chatcopilot.platforms.feishu.sender.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="cc-connect", timeout=1),
        ):
            with self.assertRaises(TimeoutError):
                feishu_sender.send_via_cc_connect([self.sample])

    def test_missing_binary_raises_runtime(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CHATCOPILOT_CC_CONNECT_BIN": ""},
            clear=False,
        ), mock.patch(
            "chatcopilot.platforms.feishu.sender.shutil.which", return_value=None
        ), mock.patch(
            "chatcopilot.platforms.feishu.sender.os.path.isfile", return_value=False
        ):
            with self.assertRaises(RuntimeError) as ctx:
                feishu_sender.send_via_cc_connect([self.sample])
        self.assertIn("cc-connect", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
