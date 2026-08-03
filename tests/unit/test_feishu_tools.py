"""通用 feishu domain 单测：URL 解析 / 命令拼装 / 校验 / 角色收敛 / 错误分类。

全部 mock 掉 lark-cli 子进程，不真正联网。
"""
from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from chatcopilot.external_tools.feishu.modules import bitable, docs, im, search, sheets
from chatcopilot.external_tools.feishu.modules.urls import (
    parse_bitable_url,
    parse_doc_url,
    parse_sheet_url,
)
from chatcopilot.external_tools.shared import lark_cli


class TestUrlParsing(unittest.TestCase):
    def test_parse_sheet_url(self) -> None:
        token, sheet = parse_sheet_url("https://x.feishu.cn/sheets/Tok123?sheet=ABC")
        self.assertEqual(token, "Tok123")
        self.assertEqual(sheet, "ABC")

    def test_parse_sheet_url_no_sheet(self) -> None:
        token, sheet = parse_sheet_url("https://x.feishu.cn/sheets/Tok123")
        self.assertEqual(token, "Tok123")
        self.assertEqual(sheet, "")

    def test_parse_bitable_url(self) -> None:
        app, table = parse_bitable_url("https://x.feishu.cn/base/Bas9?table=tblX&view=vewY")
        self.assertEqual(app, "Bas9")
        self.assertEqual(table, "tblX")

    def test_parse_doc_url(self) -> None:
        dtype, token = parse_doc_url("https://x.feishu.cn/docx/Dox1")
        self.assertEqual(dtype, "docx")
        self.assertEqual(token, "Dox1")


class TestSheetsCommands(unittest.TestCase):
    def test_read_range_builds_get(self) -> None:
        with mock.patch.object(sheets, "run_api", return_value={"code": 0}) as m:
            sheets.read_range("https://x.feishu.cn/sheets/Tok?sheet=S1", range_a1="A1:B2")
        args, kwargs = m.call_args
        self.assertEqual(args[0], "GET")
        self.assertEqual(args[1], "/sheets/v2/spreadsheets/Tok/values/S1!A1:B2")

    def test_write_range_builds_put_body(self) -> None:
        with mock.patch.object(sheets, "run_api", return_value={"code": 0}) as m:
            sheets.write_range(
                "https://x.feishu.cn/sheets/Tok?sheet=S1",
                values=[["a", "b"]], range_a1="A1:B1",
            )
        args, kwargs = m.call_args
        self.assertEqual(args[0], "PUT")
        self.assertEqual(args[1], "/sheets/v2/spreadsheets/Tok/values")
        self.assertEqual(kwargs["data"]["valueRange"]["range"], "S1!A1:B1")
        self.assertEqual(kwargs["data"]["valueRange"]["values"], [["a", "b"]])

    def test_append_builds_values_append(self) -> None:
        with mock.patch.object(sheets, "run_api", return_value={"code": 0}) as m:
            sheets.append_rows("https://x.feishu.cn/sheets/Tok?sheet=S1", values=[["x"]])
        args, _ = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].endswith("/values_append"))


class TestBitableCommands(unittest.TestCase):
    def test_resolve_requires_table(self) -> None:
        with self.assertRaises(ValueError):
            bitable.list_records("https://x.feishu.cn/base/Bas9")

    def test_add_record_posts_fields(self) -> None:
        with mock.patch.object(bitable, "run_api", return_value={"code": 0}) as m:
            bitable.add_record(
                "https://x.feishu.cn/base/Bas9?table=tblX", fields={"名称": "v"},
            )
        args, kwargs = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/bitable/v1/apps/Bas9/tables/tblX/records")
        self.assertEqual(kwargs["data"], {"fields": {"名称": "v"}})

    def test_update_requires_record_id(self) -> None:
        with self.assertRaises(ValueError):
            bitable.update_record(
                "https://x.feishu.cn/base/Bas9?table=tblX", record_id="", fields={"a": 1},
            )


class TestImSend(unittest.TestCase):
    def test_empty_receive_id_raises(self) -> None:
        with self.assertRaises(ValueError):
            im.send_message(receive_id="", receive_id_type="open_id", text="hi")

    def test_bad_receive_id_type_raises(self) -> None:
        with self.assertRaises(ValueError):
            im.send_message(receive_id="ou_1", receive_id_type="bogus", text="hi")

    def test_text_builds_content_string(self) -> None:
        with mock.patch.object(im, "run_api", return_value={"code": 0}) as m:
            im.send_message(receive_id="ou_1", receive_id_type="open_id", text="hi")
        args, kwargs = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/im/v1/messages")
        self.assertEqual(kwargs["params"], {"receive_id_type": "open_id"})
        self.assertEqual(kwargs["data"]["content"], '{"text": "hi"}')


class TestDocsCreate(unittest.TestCase):
    def test_create_uses_docs_create_shortcut(self) -> None:
        fake = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout='{"data": {"url": "https://x.feishu.cn/docx/New1", "document": {"document_id": "New1"}}}',
            stderr="",
        )
        with mock.patch.object(docs, "run_lark_cli", return_value=fake) as m:
            info = docs.create_doc(title="周报", markdown="# hi")
        cli_args = m.call_args[0][0]
        self.assertEqual(cli_args[0:2], ["docs", "+create"])
        self.assertIn("--content", cli_args)
        self.assertEqual(info["url"], "https://x.feishu.cn/docx/New1")
        self.assertEqual(info["document_id"], "New1")

    def test_append_builds_children_blocks(self) -> None:
        with mock.patch.object(docs, "run_api", return_value={"code": 0}) as m:
            docs.append_markdown(url="https://x.feishu.cn/docx/Dox1", markdown="line1\n\nline2")
        args, kwargs = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/docx/v1/documents/Dox1/blocks/Dox1/children")
        children = kwargs["data"]["children"]
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0]["block_type"], 2)


class TestSearch(unittest.TestCase):
    def test_wiki_search_posts_query(self) -> None:
        with mock.patch.object(search, "run_api", return_value={"code": 0}) as m:
            search.wiki_search(query="perf")
        args, kwargs = m.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "/wiki/v1/nodes/search")
        self.assertEqual(kwargs["data"]["query"], "perf")


class TestSharedRunApi(unittest.TestCase):
    def _fake_node(self, stdout: str, returncode: int = 0):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    def test_run_api_parses_json(self) -> None:
        with mock.patch.object(lark_cli, "resolve_lark_cli_node_args", return_value=["node", "run.js"]), \
             mock.patch.object(lark_cli, "run_lark_node", return_value=self._fake_node('{"code":0,"data":{"ok":1}}')):
            out = lark_cli.run_api("GET", "/im/v1/chats")
        self.assertEqual(out["data"]["ok"], 1)

    def test_run_api_raises_on_api_error(self) -> None:
        with mock.patch.object(lark_cli, "resolve_lark_cli_node_args", return_value=["node", "run.js"]), \
             mock.patch.object(lark_cli, "run_lark_node", return_value=self._fake_node('{"code":1254005,"msg":"no permission"}')):
            with self.assertRaises(lark_cli.LarkCliError):
                lark_cli.run_api("POST", "/bitable/v1/apps/x/tables/y/records", data={"fields": {}})

    def test_auth_error_classification(self) -> None:
        self.assertTrue(lark_cli.is_auth_error_text("missing access token"))
        with self.assertRaises(lark_cli.LarkCliAuthError):
            lark_cli.raise_lark_cli_error_for_output("permission denied: scope missing")


class TestSpecDiscovery(unittest.TestCase):
    def test_owner_gating_and_no_collision(self) -> None:
        from chatcopilot.agent.tools.registry import discover_tools

        tools = discover_tools(
            tool_packs=("feishu.document", "feishu.sheet", "feishu.bitable", "feishu.wiki", "feishu.messaging")
        )
        by_name = {t.name: t for t in tools}
        # 读工具开放
        for n in ("feishu_sheet_read", "feishu_bitable_query", "feishu_wiki_search", "feishu_drive_search", "feishu_api_get"):
            self.assertIn(n, by_name)
            self.assertIsNone(by_name[n].requires_role)
        # 写/发消息工具 owner-only
        for n in ("feishu_doc_create", "feishu_doc_append", "feishu_sheet_write", "feishu_sheet_append",
                  "feishu_bitable_add", "feishu_bitable_update", "feishu_im_send"):
            self.assertEqual(by_name[n].requires_role, "owner")

    def test_values_validation(self) -> None:
        from chatcopilot.external_tools.feishu import spec

        with self.assertRaises(ValueError):
            spec._as_2d_values("not json")
        with self.assertRaises(ValueError):
            spec._as_2d_values([1, 2, 3])
        self.assertEqual(spec._as_2d_values('[["a"],[1]]'), [["a"], [1]])


if __name__ == "__main__":
    unittest.main()
