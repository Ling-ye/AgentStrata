"""Regression tests for Feishu ACP attachment-only handling."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from chatcopilot.middleware.runtime.workspace import Workspace
from chatcopilot.middleware.acp.session_state import _make_test_session_state
from chatcopilot.middleware.acp import server as acp_server
from chatcopilot.agent.protocol import AgentResult as _AgentResult, FinalText as _FinalText
from chatcopilot.middleware.acp.private_space import (
    format_workspace_inventory,
    is_workspace_inventory_query,
)
from chatcopilot.middleware.acp.attachment_classifier import (
    ResourceKind,
    classify_resource_block,
)
from chatcopilot.middleware.acp.attachment_pipeline import (
    collect_attachment_references,
    extract_attachment_names_from_text,
    extract_prompt_parts,
    format_attachment_ack,
    format_attachment_deferred_receipt,
    format_attachment_receipt,
    format_feishu_file_size_limit_reply,
    has_task_verb,
    has_text_attachment_reference,
    import_transport_attachments,
    is_feishu_file_size_limit_error,
    is_textified_attachment_upload_only,
    normalize_cc_connect_wrapper,
    should_short_circuit_attachment_only,
)
from chatcopilot.middleware.acp.server import AcpChatAgent, _refresh_session_system_prompt
from chatcopilot.middleware.acp.server import _fallback_p2p_workspace_from_sender
from chatcopilot.middleware.acp.prompt_assembler import build_system_prompt as _build_system_prompt


def build_system_prompt(workspace: Workspace, **kwargs) -> str:
    return _build_system_prompt(platform_type="feishu", workspace=workspace, **kwargs)


class _FakeConn:
    def __init__(self) -> None:
        self.messages: list[tuple[str, Any]] = []

    async def session_update(self, *, session_id: str, update: Any) -> None:
        self.messages.append((session_id, update))


def _runtime_context(
    *,
    platform_type: str = "qq",
    tool_features: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        platform_type=platform_type,
        tool_features=tool_features,
        access=None,
        spec=None,
    )


class AttachmentGateTests(unittest.TestCase):
    def test_attachment_ack_delay_is_three_seconds(self) -> None:
        self.assertEqual(acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC, 3.0)

    def test_feishu_file_size_limit_error_is_detected(self) -> None:
        text = (
            'time=2026-05-26T17:03:24.084+08:00 level=ERROR '
            'msg="feishu: download file failed" '
            'error="feishu: resource API code=234037 msg=Downloaded file size exceeds limit."'
        )

        self.assertTrue(is_feishu_file_size_limit_error(text))

    def test_plain_large_file_text_is_not_size_limit_error(self) -> None:
        self.assertFalse(is_feishu_file_size_limit_error("这个文件太大了，帮我分析一下怎么拆分"))
        self.assertFalse(is_feishu_file_size_limit_error("Downloaded file size exceeds limit 是什么意思？"))

    def test_resource_only_prompt_short_circuits(self) -> None:
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "name": "MemoryReport_before.csv",
                    "uri": "file:///attachments/MemoryReport_before.csv",
                }
            ]
        )

        self.assertTrue(parts.has_resource)
        self.assertEqual(parts.resource_names, ["MemoryReport_before.csv"])
        self.assertTrue(should_short_circuit_attachment_only(parts))

    def test_normalize_cc_connect_wrapper_single_file_strips_text_and_lifts_resource(self) -> None:
        """Regression: cc-connect 把飞书 file 消息合成的英文包装文本（含 ``analyze``）
        必须被归一化为结构化资源引用，使下游短路链命中纯文件上传。"""
        filename = "MemoryReport_MobilePlayer_1.1.0-101-1-v1d0_2026_01_02_04_05_06.csv"
        prompt_text = (
            "Please analyze the attached file(s).\n"
            "\n"
            f"(Files saved locally, please read them: /srv/chatcopilot-workspaces/sample-bot/default/.cc-connect/attachments/{filename})"
        )
        parts = extract_prompt_parts([{"type": "text", "text": prompt_text}])

        normalized = normalize_cc_connect_wrapper(parts)

        self.assertEqual(normalized.text, "")
        self.assertEqual(normalized.resource_names, [filename])
        self.assertTrue(normalized.has_resource)
        self.assertFalse(has_task_verb(normalized.text))
        self.assertTrue(should_short_circuit_attachment_only(normalized))

    def test_normalize_cc_connect_wrapper_multi_file(self) -> None:
        filename_a = "MemoryReport_MobilePlayer_1.1.0-101-1-v1d0_2026_01_02_04_05_06.csv"
        filename_b = "MemoryReport_MobilePlayer_1.1.0-101-1-v1d0_2026_01_02_04_15_16.csv"
        prompt_text = (
            "Please analyze the attached file(s).\n"
            "\n"
            "(Files saved locally, please read them: "
            f"/srv/fixtures/.cc-connect/attachments/{filename_a}, "
            f"/srv/fixtures/.cc-connect/attachments/{filename_b})"
        )
        parts = extract_prompt_parts([{"type": "text", "text": prompt_text}])

        normalized = normalize_cc_connect_wrapper(parts)

        self.assertEqual(normalized.resource_names, [filename_a, filename_b])
        self.assertTrue(should_short_circuit_attachment_only(normalized))

    def test_normalize_cc_connect_wrapper_preserves_real_user_text(self) -> None:
        """Regression: 飞书消息若同时带用户自然语言，归一化只剥离协议包装段，
        保留用户原话，让下游 has_task_verb 继续根据用户真意决定是否进 LLM。"""
        filename = "MemoryReport_xxx.csv"
        prompt_text = (
            "顺便对比下昨天那份。\n"
            "Please analyze the attached file(s).\n"
            "\n"
            f"(Files saved locally, please read them: /srv/fixtures/.cc-connect/attachments/{filename})"
        )
        parts = extract_prompt_parts([{"type": "text", "text": prompt_text}])

        normalized = normalize_cc_connect_wrapper(parts)

        self.assertEqual(normalized.resource_names, [filename])
        self.assertEqual(normalized.text, "顺便对比下昨天那份。")
        self.assertTrue(has_task_verb(normalized.text))
        self.assertFalse(should_short_circuit_attachment_only(normalized))

    def test_normalize_cc_connect_wrapper_noop_on_plain_user_request(self) -> None:
        """反例：用户自己说 ``请分析这个 csv`` 不是协议包装，归一化层必须原样放行，
        不能把用户真实指令当成包装吞掉。"""
        prompt_text = "请分析这个 csv"
        parts = extract_prompt_parts([{"type": "text", "text": prompt_text}])

        normalized = normalize_cc_connect_wrapper(parts)

        self.assertIs(normalized, parts)
        self.assertEqual(normalized.text, prompt_text)
        self.assertEqual(normalized.resource_names, [])

    def test_normalize_cc_connect_wrapper_noop_when_resource_block_present(self) -> None:
        """反例：上游已经传了结构化 resource block，归一化层不再覆盖，避免双重处理。"""
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "name": "MemoryReport_before.csv",
                    "uri": "file:///attachments/MemoryReport_before.csv",
                },
                {
                    "type": "text",
                    "text": "Please analyze the attached file(s).\n\n(Files saved locally, please read them: /tmp/foo.csv)",
                },
            ]
        )

        normalized = normalize_cc_connect_wrapper(parts)

        self.assertIs(normalized, parts)
        self.assertEqual(normalized.resource_names, ["MemoryReport_before.csv"])

    def test_format_attachment_receipt_single_file(self) -> None:
        text = format_attachment_receipt(["MemoryReport.csv"])
        self.assertIn("已收到附件：MemoryReport.csv", text)
        self.assertIn("正在保存到你的私人空间", text)
        self.assertIn("稍候我会再发一条确认", text)

    def test_format_attachment_receipt_multi_file(self) -> None:
        text = format_attachment_receipt(["file_a.csv", "file_b.csv"])
        self.assertIn("已收到附件：file_a.csv、file_b.csv", text)
        self.assertIn("正在保存到你的私人空间", text)

    def test_format_attachment_receipt_dedupes_and_basenames(self) -> None:
        text = format_attachment_receipt(
            [
                "/tmp/.cc-connect/attachments/foo.csv",
                "foo.csv",
                "",
            ]
        )
        self.assertIn("已收到附件：foo.csv", text)
        self.assertEqual(text.count("foo.csv"), 1)

    def test_format_attachment_receipt_falls_back_when_empty(self) -> None:
        text = format_attachment_receipt([])
        self.assertTrue(text.strip())
        self.assertIn("已收到", text)

    def test_attachment_only_emits_eager_final_ack(self) -> None:
        """Regression: 短路 6 必须**同步**在本 turn 内发完最终 ack，不依赖
        debounce 异步任务——已验证 cc-connect 在 ACP ``session/prompt`` 返回
        ``end_turn`` 之后会丢弃后续 ``session_update``，异步 ack 用户永远看不到。"""

        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "p2p_ou_test",
                        chat_kind="p2p",
                        chat_id="oc_private",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    (default_ws.attachments / "MemoryReport.csv").write_text("x", encoding="utf-8")

                    session = _make_test_session_state(
                        session_id="sid",
                        workspace=user_ws,
                        system_prompt=build_system_prompt(user_ws),
                    )

                    def fail_run_task(task, **kwargs) -> None:
                        raise AssertionError(f"run_task should not be called: {task}")

                    session.session.run_task = fail_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    await agent._prompt_locked(
                        [
                            {
                                "type": "resource_link",
                                "name": "MemoryReport.csv",
                                "uri": "file:///attachments/MemoryReport.csv",
                            }
                        ],
                        "sid",
                        "mid",
                    )

                    # 立即应有 1 条最终 ack，含完整保存路径与累计列表。
                    self.assertEqual(len(agent._conn.messages), 1)
                    _sid, final_text = agent._conn.messages[0]
                    self.assertIn(
                        "文件已保存到你的私人空间：attachments/MemoryReport.csv。",
                        final_text,
                    )
                    self.assertIn("请告诉我下一步要做什么。", final_text)
                    # eager 路径不应再 schedule 异步 task
                    self.assertNotIn("sid", agent._attachment_ack_tasks)
                    self.assertNotIn("sid", agent._attachment_ack_resource_names)
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_feishu_file_size_limit_error_short_circuits_without_agent(self) -> None:
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    user_ws = Workspace(
                        root=Path(tmp) / "p2p_ou_test",
                        chat_kind="p2p",
                        chat_id="oc_private",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    session = _make_test_session_state(
                        session_id="sid",
                        workspace=user_ws,
                        system_prompt=build_system_prompt(user_ws),
                    )

                    def fail_run_task(task, **kwargs) -> None:
                        raise AssertionError(f"run_task should not be called: {task}")

                    session.session.run_task = fail_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {"sid": asyncio.create_task(asyncio.sleep(60))}
                    agent._attachment_ack_resource_names = {"sid": ["too_large.zip"]}
                    prompt_text = (
                        'time=2026-05-26T17:03:24.084+08:00 level=ERROR '
                        'msg="feishu: download file failed" '
                        'error="feishu: resource API code=234037 '
                        'msg=Downloaded file size exceeds limit."'
                    )

                    await agent._prompt_locked(
                        [{"type": "text", "text": prompt_text}],
                        "sid",
                        "mid",
                    )

                    self.assertEqual(len(agent._conn.messages), 1)
                    _sid, final_text = agent._conn.messages[0]
                    self.assertEqual(final_text, format_feishu_file_size_limit_reply())
                    self.assertIn("文件太大", final_text)
                    self.assertNotIn("sid", agent._attachment_ack_tasks)
                    self.assertNotIn("sid", agent._attachment_ack_resource_names)
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_file_transfer_with_explicit_diff_request_does_not_short_circuit(self) -> None:
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "name": "MemoryReport_before.csv",
                    "uri": "file:///attachments/MemoryReport_before.csv",
                },
                {
                    "type": "resource_link",
                    "name": "MemoryReport_after.csv",
                    "uri": "file:///attachments/MemoryReport_after.csv",
                },
                {"type": "text", "text": "做 diff"},
            ]
        )

        self.assertTrue(parts.has_resource)
        self.assertTrue(has_task_verb(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_modulealloc_resources_with_explicit_diff_do_not_short_circuit(self) -> None:
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "name": "v3-sample_scene.moduleAlloc",
                    "uri": "file:///attachments/v3-sample_scene.moduleAlloc",
                },
                {
                    "type": "resource_link",
                    "name": "v4-sample_scene.moduleAlloc",
                    "uri": "file:///attachments/v4-sample_scene.moduleAlloc",
                },
                {"type": "text", "text": "给我diff这两个.moduleAlloc文件"},
            ]
        )

        self.assertTrue(parts.has_resource)
        self.assertEqual(parts.resource_names, ["v3-sample_scene.moduleAlloc", "v4-sample_scene.moduleAlloc"])
        self.assertTrue(has_task_verb(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_text_only_explicit_diff_request_is_not_short_circuited(self) -> None:
        parts = extract_prompt_parts([{"type": "text", "text": "用刚才两个文件做 diff"}])

        self.assertFalse(parts.has_resource)
        self.assertTrue(has_task_verb(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_migration_request_is_explicit_task(self) -> None:
        text = (
            "给我把 https://example.feishu.cn/sheets/source 的数据，"
            "转移到 SampleGame-内存数据库"
        )
        parts = extract_prompt_parts([{"type": "text", "text": text}])

        self.assertFalse(parts.has_resource)
        self.assertTrue(has_task_verb(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_textified_attachment_with_sync_task_does_not_short_circuit(self) -> None:
        text = r"把 @c:\fixtures\source.xlsx 同步到 SampleGame-内存数据库"
        parts = extract_prompt_parts([{"type": "text", "text": text}])

        self.assertFalse(parts.has_resource)
        self.assertTrue(has_task_verb(parts.text))
        self.assertTrue(has_text_attachment_reference(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_http_url_is_not_treated_as_textified_attachment_path(self) -> None:
        text = "你用webfetch访问下https://tarkov.dev/boss/cultist-warrior"
        parts = extract_prompt_parts([{"type": "text", "text": text}])

        self.assertFalse(parts.has_resource)
        self.assertFalse(has_text_attachment_reference(parts.text))
        self.assertFalse(is_textified_attachment_upload_only(parts.text))
        self.assertEqual(extract_attachment_names_from_text(parts.text), [])
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_http_file_url_does_not_hide_real_local_attachment(self) -> None:
        text = (
            "参考 https://example.com/reports/source.csv，"
            r"再处理 @c:\fixtures\actual.csv"
        )

        self.assertTrue(has_text_attachment_reference(text))
        self.assertEqual(extract_attachment_names_from_text(text), ["actual.csv"])

    def test_arbitrary_file_resource_short_circuits(self) -> None:
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "name": "render_frame_time.json",
                    "uri": "file:///attachments/render_frame_time.json",
                }
            ]
        )

        self.assertTrue(parts.has_resource)
        self.assertEqual(parts.resource_names, ["render_frame_time.json"])
        self.assertTrue(should_short_circuit_attachment_only(parts))

    def test_nested_resource_block_short_circuits(self) -> None:
        parts = extract_prompt_parts(
            [
                {
                    "type": "message",
                    "content": {
                        "attachments": [
                            {
                                "file": {
                                    "filename": "capture_before.memory_report",
                                    "uri": "file:///attachments/capture_before.memory_report",
                                }
                            },
                            {
                                "file": {
                                    "filename": "capture_after.memory_report",
                                    "uri": "file:///attachments/capture_after.memory_report",
                                }
                            },
                        ]
                    },
                }
            ]
        )

        self.assertTrue(parts.has_resource)
        self.assertEqual(parts.resource_names, ["capture_before.memory_report", "capture_after.memory_report"])
        self.assertTrue(should_short_circuit_attachment_only(parts))

    def test_feishu_url_resource_link_is_not_treated_as_attachment(self) -> None:
        """Regression: 飞书 cc-connect 把消息里的 https://xxx.feishu.cn 链接预览
        封成 ACP resource_link 推过来，URL 末段不应被截成 ``example.feishu.cn``
        当成"附件文件名"，否则会把"已收到附件：example.feishu.cn"误回给用户。
        """
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "uri": "https://example.feishu.cn/sheets/abc123",
                },
                {
                    "type": "text",
                    "text": (
                        "把版本性能数据源（内存）"
                        "数据迁移到 SampleGame-内存数据库中"
                    ),
                },
            ]
        )

        self.assertEqual(parts.resource_names, [])
        self.assertEqual(collect_attachment_references(parts, parts.text), [])
        self.assertTrue(has_task_verb(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_feishu_bare_domain_name_is_not_treated_as_attachment(self) -> None:
        """Regression: 即便 ACP block 的 ``name`` 字段被设成纯域名 ``example.feishu.cn``，
        也必须识别为非附件，避免它被当作合法 basename 写入回执文案。"""
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "name": "example.feishu.cn",
                    "uri": "https://example.feishu.cn/",
                },
                {"type": "text", "text": "请把这个表迁移到 SampleGame-内存数据库"},
            ]
        )

        self.assertEqual(parts.resource_names, [])
        self.assertEqual(collect_attachment_references(parts, parts.text), [])
        self.assertTrue(has_task_verb(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_real_attachment_alongside_feishu_url_keeps_only_real_file(self) -> None:
        """混合 block：飞书 URL resource + 真实 csv resource，应只保留真实 csv。"""
        parts = extract_prompt_parts(
            [
                {
                    "type": "resource_link",
                    "uri": "https://example.feishu.cn/sheets/abc123",
                },
                {
                    "type": "resource_link",
                    "name": "MemoryReport_after.csv",
                    "uri": "file:///attachments/MemoryReport_after.csv",
                },
                {"type": "text", "text": "对比一下这两个文件"},
            ]
        )

        self.assertEqual(parts.resource_names, ["MemoryReport_after.csv"])
        self.assertEqual(
            collect_attachment_references(parts, parts.text),
            ["MemoryReport_after.csv"],
        )

    def test_task_verb_does_not_match_filename_substrings(self) -> None:
        self.assertFalse(has_task_verb("desktop_capture.csv"))
        self.assertFalse(has_task_verb("difference_report.csv"))
        self.assertFalse(has_task_verb("topology_snapshot.json"))

    # ------------------------------------------------------------------
    # ResourceKind classifier 回归用例（覆盖此前导致"URL 被识别成文件"
    # bug 反复出现的所有上游字段命名 / 类型形态）
    # ------------------------------------------------------------------
    def test_classifier_web_url_via_href_field(self) -> None:
        """上游用 ``href`` 字段携 URL —— 老代码 _RESOURCE_SOURCE_KEYS 只看 path/uri/url
        会漏检。classifier 必须把 href 也认作 source-like。"""
        result = classify_resource_block(
            {
                "type": "resource_link",
                "name": "Example Sheets",
                "href": "https://example.feishu.cn/sheets/abc",
            }
        )
        self.assertEqual(result.kind, ResourceKind.WEB_URL)
        self.assertEqual(result.name, "")

    def test_classifier_web_url_via_nested_source(self) -> None:
        """URL 嵌在 ``source.uri`` 里。"""
        result = classify_resource_block(
            {
                "type": "resource_link",
                "name": "x",
                "source": {"uri": "https://example.feishu.cn/docx/abc"},
            }
        )
        self.assertEqual(result.kind, ResourceKind.WEB_URL)

    def test_classifier_web_url_via_target_field(self) -> None:
        """部分上游用 ``target`` 当 URL 字段。"""
        result = classify_resource_block(
            {
                "type": "resource_link",
                "name": "doc",
                "target": "https://example.com/x",
            }
        )
        self.assertEqual(result.kind, ResourceKind.WEB_URL)

    def test_classifier_bare_hostname_name_only(self) -> None:
        """block 只有 name 是裸 hostname，**没有**任何 source 字段。
        老代码会把 ``.cn`` 当合法扩展名放行；classifier 必须用 TLD 黑名单 +
        hostname 形态把它拒掉。"""
        result = classify_resource_block(
            {"type": "resource_link", "name": "example.feishu.cn"}
        )
        self.assertEqual(result.kind, ResourceKind.WEB_URL)
        self.assertEqual(result.name, "")

    def test_classifier_link_type_with_hostname(self) -> None:
        """显式 ``type=link`` 即便没有 URL 字段也必须判为 WEB_URL。"""
        result = classify_resource_block(
            {"type": "link", "name": "x.com"}
        )
        self.assertEqual(result.kind, ResourceKind.WEB_URL)

    def test_classifier_legacy_module_alloc_remains_file(self) -> None:
        """合法多段文件名 ``v3-sample_scene.moduleAlloc`` 必须保持 FILE，
        避免 hostname-like 启发式误伤。"""
        result = classify_resource_block(
            {
                "type": "resource_link",
                "name": "v3-sample_scene.moduleAlloc",
                "uri": "file:///attachments/v3-sample_scene.moduleAlloc",
            }
        )
        self.assertEqual(result.kind, ResourceKind.FILE)
        self.assertEqual(result.name, "v3-sample_scene.moduleAlloc")

    def test_classifier_unknown_extension_passes_when_not_hostname(self) -> None:
        """扩展名既不在文件白名单也不在 TLD 黑名单，整串又不像 hostname
        （多段下划线 + 数字），按容错放行成 FILE。"""
        result = classify_resource_block(
            {
                "type": "resource_link",
                "name": "report_v2.metrics",
                "uri": "file:///attachments/report_v2.metrics",
            }
        )
        self.assertEqual(result.kind, ResourceKind.FILE)
        self.assertEqual(result.name, "report_v2.metrics")

    def test_format_attachment_deferred_receipt_dedupes_and_filters(self) -> None:
        """deferred receipt 是 LLM 兜底路径上的占位文案。即便上游 classifier
        失效把 hostname 漏放进来，这里的 _sanitize_display_names 也必须
        把 ``example.feishu.cn`` 之类的 WEB_URL-like 名字过滤掉。"""
        text = format_attachment_deferred_receipt(
            [
                "/tmp/.cc-connect/attachments/MemoryReport.csv",
                "MemoryReport.csv",
                "example.feishu.cn",
                "",
            ]
        )
        self.assertIn("已收到附件：MemoryReport.csv，", text)
        self.assertIn("正在保存到你的私人空间", text)
        self.assertIn("重新发起 diff", text)
        self.assertEqual(text.count("MemoryReport.csv"), 1)
        self.assertNotIn("example.feishu.cn", text)

    def test_server_feishu_url_with_task_verb_does_not_emit_attachment_receipt(self) -> None:
        """端到端回归：飞书把 https://xxx.feishu.cn 包成 resource_link block
        与任务文本一起推过来时，assistant 不应该回 ``已收到附件：
        example.feishu.cn`` 之类的占位文案；应直接进 LLM turn 处理任务。"""

        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            original_refresh = acp_server._refresh_session_system_prompt
            acp_server.update_agent_message_text = lambda text: text
            acp_server._refresh_session_system_prompt = lambda _session: None
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    user_ws = Workspace(
                        root=Path(tmp) / "p2p_ou_test",
                        chat_kind="p2p",
                        chat_id="oc_private",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    session = _make_test_session_state(
                        session_id="sid",
                        workspace=user_ws,
                        system_prompt=build_system_prompt(user_ws),
                    )

                    captured: list[Any] = []

                    async def fake_run_agent_turn(*args: Any, **kwargs: Any):
                        captured.append((args, kwargs))
                        return acp_server.PromptResponse(
                            stop_reason="end_turn", user_message_id=None
                        )

                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}
                    agent._run_agent_turn = fake_run_agent_turn  # type: ignore[method-assign]

                    await agent._prompt_locked(
                        [
                            {
                                "type": "resource_link",
                                "uri": "https://example.feishu.cn/sheets/abc123",
                            },
                            {
                                "type": "text",
                                "text": (
                                    "把版本性能数据源（内存）"
                                    "数据迁移到 SampleGame-内存数据库中"
                                ),
                            },
                        ],
                        "sid",
                        "mid",
                    )

                    # 必须直接交给 LLM turn，不应产生任何占位文案。
                    self.assertEqual(len(captured), 1)
                    self.assertEqual(agent._conn.messages, [])
            finally:
                acp_server.update_agent_message_text = original_update
                acp_server._refresh_session_system_prompt = original_refresh

        asyncio.run(run_case())

    def test_attachment_ack_confirms_saved_without_analysis_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(
                root=Path(tmp),
                chat_kind="p2p",
                chat_id=None,
                user_id="ou_test",
                user_name="tester",
            ).ensure()
            (ws.attachments / "file_a.csv").write_text("a", encoding="utf-8")
            (ws.attachments / "file_b.csv").write_text("b", encoding="utf-8")

            text = format_attachment_ack(ws, ["file_a.csv", "file_b.csv"])

        self.assertIn("文件已保存到你的私人空间：attachments/file_a.csv、attachments/file_b.csv。", text)
        self.assertIn("你当前累计 2 个已保存文件：", text)
        self.assertIn("- attachments/file_a.csv", text)
        self.assertIn("- attachments/file_b.csv", text)
        self.assertIn("请告诉我下一步要做什么。", text)
        self.assertNotIn("做 diff", text)
        self.assertNotIn("跑趋势", text)
        self.assertNotIn("top 10", text)

    def test_workspace_inventory_query_intent(self) -> None:
        self.assertTrue(is_workspace_inventory_query("我的空间有哪些文件？"))
        self.assertTrue(is_workspace_inventory_query("私人空间的内容"))
        self.assertTrue(is_workspace_inventory_query("列出附件"))
        self.assertTrue(is_workspace_inventory_query("下载数据有哪些？"))
        self.assertTrue(is_workspace_inventory_query("上传文件有哪些？"))
        self.assertFalse(is_workspace_inventory_query("清空我的空间"))
        self.assertFalse(is_workspace_inventory_query("分析我的空间里的文件"))

    def test_workspace_inventory_lists_saved_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(
                root=Path(tmp),
                chat_kind="p2p",
                chat_id=None,
                user_id="ou_test",
                user_name="tester",
            ).ensure()
            (ws.attachments / "v3-sample_scene.moduleAlloc").write_text("x", encoding="utf-8")
            (ws.attachments / "v4-sample_scene.moduleAlloc").write_text("x", encoding="utf-8")

            text = format_workspace_inventory(ws)

        self.assertIn("你的私人空间目前共有 2 个文件：", text)
        self.assertIn("附件：2 个文件", text)
        self.assertIn("- attachments/v3-sample_scene.moduleAlloc", text)
        self.assertIn("- attachments/v4-sample_scene.moduleAlloc", text)
        self.assertIn("分析产物：0 个文件", text)
        self.assertNotIn("全部为空", text)

    def test_capability_prompt_fragments_are_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(
                root=Path(tmp),
                chat_kind="p2p",
                chat_id=None,
                user_id="ou_test",
                user_name="tester",
            ).ensure()

            prompt = build_system_prompt(
                ws,
                capability_prompt_fragments=("同步数据到示例目标时使用 long_running_export。",),
            )

        self.assertIn("当前可用能力", prompt)
        self.assertIn("同步数据到示例目标", prompt)

    def test_extracts_filename_from_text_attachment_hint(self) -> None:
        names = extract_attachment_names_from_text(
            "示例用户（示例别名）:\n[文件] v3-sample_scene.moduleAlloc"
        )

        self.assertEqual(names, ["v3-sample_scene.moduleAlloc"])

    def test_textified_local_path_upload_short_circuits_without_analysis(self) -> None:
        text = r"@c:\fixtures\示例目录\v3-sample_scene.moduleAlloc xcode数据"
        parts = extract_prompt_parts([{"type": "text", "text": text}])

        self.assertFalse(parts.has_resource)
        self.assertFalse(has_task_verb(parts.text))
        self.assertTrue(has_text_attachment_reference(parts.text))
        self.assertTrue(should_short_circuit_attachment_only(parts))
        self.assertEqual(extract_attachment_names_from_text(text), ["v3-sample_scene.moduleAlloc"])

    def test_reply_textified_memoryreport_csv_is_upload_only(self) -> None:
        text = (
            "回复 示例用户（示例别名）:\u00a0\n"
            "[文件] MemoryReport_MobilePlayer_1.2.0-102-1-v1d0_2026_01_03_04_05_06.csv"
        )
        parts = extract_prompt_parts([{"type": "text", "text": text}])

        self.assertTrue(is_textified_attachment_upload_only(parts.text))
        self.assertFalse(has_task_verb(parts.text))
        self.assertTrue(should_short_circuit_attachment_only(parts))
        self.assertEqual(
            extract_attachment_names_from_text(text),
            ["MemoryReport_MobilePlayer_1.2.0-102-1-v1d0_2026_01_03_04_05_06.csv"],
        )

    def test_textified_local_path_with_explicit_task_does_not_short_circuit(self) -> None:
        text = r"分析 @c:\fixtures\示例目录\v3-sample_scene.moduleAlloc"
        parts = extract_prompt_parts([{"type": "text", "text": text}])

        self.assertFalse(parts.has_resource)
        self.assertTrue(has_task_verb(parts.text))
        self.assertTrue(has_text_attachment_reference(parts.text))
        self.assertFalse(should_short_circuit_attachment_only(parts))

    def test_imports_cc_connect_default_inbox_to_user_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default_ws = Workspace(
                root=root / "default",
                chat_kind=None,
                chat_id=None,
            ).ensure()
            user_ws = Workspace(
                root=root / "group_oc_team" / "user_ou_test",
                chat_kind="group",
                chat_id="oc_team",
                user_id="ou_test",
                user_name="tester",
            ).ensure()
            incoming = default_ws.attachments / "v3-sample_scene.moduleAlloc"
            incoming.write_text("x", encoding="utf-8")

            imported = import_transport_attachments(user_ws, ["v3-sample_scene.moduleAlloc"])

            self.assertEqual(imported, ["v3-sample_scene.moduleAlloc"])
            self.assertTrue((user_ws.attachments / "v3-sample_scene.moduleAlloc").is_file())
            self.assertFalse(incoming.exists())

    def test_imports_cc_connect_default_inbox_to_p2p_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default_ws = Workspace(
                root=root / "default",
                chat_kind=None,
                chat_id=None,
            ).ensure()
            user_ws = Workspace(
                root=root / "p2p_ou_test",
                chat_kind="p2p",
                chat_id="oc_private",
                user_id="ou_test",
                user_name="tester",
            ).ensure()
            incoming = default_ws.attachments / "MemoryReport.csv"
            incoming.write_text("x", encoding="utf-8")

            imported = import_transport_attachments(user_ws, ["MemoryReport.csv"])

            self.assertEqual(imported, ["MemoryReport.csv"])
            self.assertTrue((user_ws.attachments / "MemoryReport.csv").is_file())
            self.assertFalse(incoming.exists())

    def test_default_workspace_textified_attachment_recovers_owner_p2p_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            default_ws = Workspace(
                root=root / "default",
                chat_kind="p2p",
                chat_id=None,
                user_id=None,
                user_name=None,
            ).ensure()
            incoming = default_ws.attachments / "0521ifix.moduleAlloc"
            incoming.write_text("x", encoding="utf-8")

            with mock.patch.dict(
                os.environ,
                {"CHATCOPILOT_ADD_OWNER_NAMES": "示例用户"},
                clear=False,
            ):
                recovered_ws = _fallback_p2p_workspace_from_sender(
                    default_ws,
                    "示例用户（示例别名）:\n[文件] 0521ifix.moduleAlloc",
                )

            self.assertIsNotNone(recovered_ws)
            assert recovered_ws is not None
            self.assertEqual(recovered_ws.chat_kind, "p2p")
            self.assertEqual(recovered_ws.user_id, "name_示例用户")
            self.assertTrue(recovered_ws.root.name.startswith("p2p_name_示例用户"))

            imported = import_transport_attachments(recovered_ws, ["0521ifix.moduleAlloc"])

            self.assertEqual(imported, ["0521ifix.moduleAlloc"])
            self.assertTrue((recovered_ws.attachments / "0521ifix.moduleAlloc").is_file())
            self.assertFalse(incoming.exists())

    def test_memoryreport_csv_textified_upload_from_default_never_enters_agent(self) -> None:
        async def run_case() -> None:
            original_delay = acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC
            original_update = acp_server.update_agent_message_text
            original_build_session = acp_server._build_session_for_workspace
            acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = 0.02
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind="p2p",
                        chat_id=None,
                        user_id=None,
                        user_name=None,
                    ).ensure()
                    filename = "MemoryReport_MobilePlayer_1.2.0-102-1-v1d0_2026_01_03_04_05_06.csv"
                    (default_ws.attachments / filename).write_text("frame,cost\n1,2", encoding="utf-8")

                    session = _make_test_session_state(session_id="sid", workspace=default_ws, system_prompt=build_system_prompt(default_ws),)

                    def fail_run_task(task, **kwargs) -> None:
                        raise AssertionError(f"run_task should not be called: {task}")

                    session.session.run_task = fail_run_task  # type: ignore[method-assign]

                    def fake_build_session(**kwargs: Any):
                        rebuilt = _make_test_session_state(session_id=kwargs["session_id"], workspace=kwargs["ws"], system_prompt=build_system_prompt(kwargs["ws"]),)
                        rebuilt.session.run_task = fail_run_task  # type: ignore[method-assign]
                        return rebuilt

                    acp_server._build_session_for_workspace = fake_build_session  # type: ignore[assignment]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}
                    agent._agent_runtime = None
                    agent._runtime = None

                    with mock.patch.dict(
                        os.environ,
                        {"CHATCOPILOT_ADD_OWNER_NAMES": "示例用户"},
                        clear=False,
                    ):
                        await agent._prompt_locked(
                            [
                                {
                                    "type": "text",
                                    "text": (
                                        f"回复 示例用户（示例别名）:\u00a0\n"
                                        f"[文件] {filename}"
                                    ),
                                }
                            ],
                            "sid",
                            "mid",
                        )

                    recovered_session = agent._sessions["sid"]
                    self.assertNotEqual(recovered_session.workspace.root, default_ws.root)
                    self.assertTrue(
                        recovered_session.workspace.root.name.startswith(
                            "p2p_name_示例用户"
                        )
                    )
                    self.assertFalse((default_ws.attachments / filename).exists())
                    self.assertTrue((recovered_session.workspace.attachments / filename).is_file())

                    self.assertEqual(len(agent._conn.messages), 1)
                    _sid, text = agent._conn.messages[0]
                    self.assertIn(f"文件已保存到你的私人空间：attachments/{filename}。", text)
                    self.assertIn("请告诉我下一步要做什么。", text)
            finally:
                acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = original_delay
                acp_server.update_agent_message_text = original_update
                acp_server._build_session_for_workspace = original_build_session

        asyncio.run(run_case())

    def test_p2p_attachment_only_delays_ack_and_does_not_enter_agent_turn(self) -> None:
        async def run_case() -> None:
            original_delay = acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC
            original_update = acp_server.update_agent_message_text
            acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = 0.02
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "p2p_ou_test",
                        chat_kind="p2p",
                        chat_id="oc_private",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    (default_ws.attachments / "MemoryReport.csv").write_text("x", encoding="utf-8")

                    session = _make_test_session_state(session_id="sid", workspace=user_ws, system_prompt=build_system_prompt(user_ws),)

                    def fail_run_task(task, **kwargs) -> None:
                        raise AssertionError(f"run_task should not be called: {task}")

                    session.session.run_task = fail_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    await agent._prompt_locked(
                        [
                            {
                                "type": "resource_link",
                                "name": "MemoryReport.csv",
                                "uri": "file:///attachments/MemoryReport.csv",
                            }
                        ],
                        "sid",
                        "mid",
                    )

                    self.assertFalse((default_ws.attachments / "MemoryReport.csv").exists())
                    self.assertTrue((user_ws.attachments / "MemoryReport.csv").is_file())
                    self.assertEqual(len(agent._conn.messages), 1)
                    _sid, text = agent._conn.messages[0]
                    self.assertIn("文件已保存到你的私人空间：attachments/MemoryReport.csv。", text)
                    self.assertIn("请告诉我下一步要做什么。", text)
                    self.assertEqual(session._messages[-2]["role"], "user")
                    self.assertEqual(session._messages[-1]["role"], "assistant")
                    self.assertIn("attachments/MemoryReport.csv", session._messages[-1]["content"])
            finally:
                acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = original_delay
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_cc_connect_wrapper_prompt_short_circuits_end_to_end(self) -> None:
        """Regression: 还原线上 ACP 实际入参——cc-connect 把飞书 file 消息合成
        ``Please analyze the attached file(s).`` 包装文本传过来。归一化层接住
        以后整条链应直接命中纯附件短路，不允许触达 ``run_task``。"""

        async def run_case() -> None:
            original_delay = acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC
            original_update = acp_server.update_agent_message_text
            acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = 0.02
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "p2p_ou_test",
                        chat_kind="p2p",
                        chat_id="oc_private",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    filename = (
                        "MemoryReport_MobilePlayer_1.1.0-101-1-v1d0_2026_01_02_04_05_06.csv"
                    )
                    (default_ws.attachments / filename).write_text("frame,cost\n1,2", encoding="utf-8")

                    session = _make_test_session_state(
                        session_id="sid",
                        workspace=user_ws,
                        system_prompt=build_system_prompt(user_ws),
                    )

                    def fail_run_task(task, **kwargs) -> None:
                        raise AssertionError(f"run_task should not be called: {task}")

                    session.session.run_task = fail_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    prompt_text = (
                        "Please analyze the attached file(s).\n"
                        "\n"
                        f"(Files saved locally, please read them: {default_ws.attachments}/{filename})"
                    )
                    await agent._prompt_locked(
                        [{"type": "text", "text": prompt_text}],
                        "sid",
                        "mid",
                    )

                    self.assertFalse((default_ws.attachments / filename).exists())
                    self.assertTrue((user_ws.attachments / filename).is_file())

                self.assertEqual(len(agent._conn.messages), 1)
                _sid, text = agent._conn.messages[0]
                self.assertIn(f"文件已保存到你的私人空间：attachments/{filename}。", text)
                self.assertIn("请告诉我下一步要做什么。", text)
            finally:
                acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = original_delay
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_feishu_group_attachment_only_imports_to_group_user_workspace(self) -> None:
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "group_group-001" / "user_user-001",
                        chat_kind="group",
                        chat_id="group-001",
                        user_id="user-001",
                        user_name="Lingye",
                    ).ensure()
                    filename = "d91761343ad3ca61d1eddd92ee0971cc.jpg"
                    (default_ws.attachments / filename).write_bytes(b"fake-jpg")
                    session = _make_test_session_state(session_id="sid", workspace=user_ws, system_prompt=build_system_prompt(user_ws),)

                    def fail_run_task(task, **kwargs) -> None:
                        raise AssertionError(f"run_task should not be called: {task}")

                    session.session.run_task = fail_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}
                    agent._runtime = _runtime_context(
                        platform_type="feishu",
                        tool_features=("chat.file_uploads", "chat.private_workspace")
                    )

                    await agent._prompt_locked(
                        [
                            {
                                "type": "resource_link",
                                "name": filename,
                                "uri": f"file:///attachments/{filename}",
                            }
                        ],
                        "sid",
                        "mid",
                    )

                    self.assertFalse((default_ws.attachments / filename).exists())
                    self.assertTrue((user_ws.attachments / filename).is_file())

                self.assertEqual(len(agent._conn.messages), 1)
                _sid, text = agent._conn.messages[0]
                self.assertIn(f"文件已保存到你的私人空间：attachments/{filename}。", text)
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_feishu_without_user_files_capability_does_not_import_attachments(self) -> None:
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "group_group-001" / "user_user-001",
                        chat_kind="group",
                        chat_id="group-001",
                        user_id="user-001",
                        user_name="Lingye",
                    ).ensure()
                    filename = "d91761343ad3ca61d1eddd92ee0971cc.jpg"
                    (default_ws.attachments / filename).write_bytes(b"fake-jpg")
                    session = _make_test_session_state(session_id="sid", workspace=user_ws, system_prompt=build_system_prompt(user_ws),)
                    seen_user_text: list[str] = []

                    def fake_run_task(task, *, on_event, **kwargs):
                        seen_user_text.append(task.text)
                        on_event(_FinalText(text="进入普通对话流程"))
                        return _AgentResult(final_text="进入普通对话流程", stop_reason="end_turn")

                    session.session.run_task = fake_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}
                    agent._runtime = _runtime_context(
                        platform_type="feishu",
                        tool_features=(),
                    )

                    await agent._prompt_locked(
                        [
                            {"type": "text", "text": "分析这张图"},
                            {
                                "type": "resource_link",
                                "name": filename,
                                "uri": f"file:///attachments/{filename}",
                            },
                        ],
                        "sid",
                        "mid",
                    )

                    self.assertTrue((default_ws.attachments / filename).is_file())
                    self.assertFalse((user_ws.attachments / filename).exists())
                    self.assertEqual(len(seen_user_text), 1)

                self.assertEqual(len(agent._conn.messages), 1)
                _sid, text = agent._conn.messages[0]
                self.assertIn("进入普通对话流程", text)
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_resource_task_imports_attachments_then_enters_agent_turn(self) -> None:
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "group_oc_team" / "user_ou_test",
                        chat_kind="group",
                        chat_id="oc_team",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    (default_ws.attachments / "v3-sample_scene.moduleAlloc").write_text("old", encoding="utf-8")
                    (default_ws.attachments / "v4-sample_scene.moduleAlloc").write_text("new", encoding="utf-8")

                    session = _make_test_session_state(session_id="sid", workspace=user_ws, system_prompt=build_system_prompt(user_ws),)
                    seen_user_text: list[str] = []

                    def fake_run_task(task, *, on_event, **kwargs):
                        seen_user_text.append(task.text)
                        on_event(_FinalText(text="已进入工具流程"))
                        return _AgentResult(final_text="已进入工具流程", stop_reason="end_turn")

                    session.session.run_task = fake_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    await agent._prompt_locked(
                        [
                            {
                                "type": "resource_link",
                                "name": "v3-sample_scene.moduleAlloc",
                                "uri": "file:///attachments/v3-sample_scene.moduleAlloc",
                            },
                            {
                                "type": "resource_link",
                                "name": "v4-sample_scene.moduleAlloc",
                                "uri": "file:///attachments/v4-sample_scene.moduleAlloc",
                            },
                            {"type": "text", "text": "给我diff这两个.moduleAlloc文件"},
                        ],
                        "sid",
                        "mid",
                    )

                    self.assertEqual(seen_user_text, [
                        "给我diff这两个.moduleAlloc文件\n"
                        "[资源引用: v3-sample_scene.moduleAlloc]\n"
                        "[资源引用: v4-sample_scene.moduleAlloc]"
                    ])
                    self.assertEqual(len(agent._conn.messages), 1)
                    _sid, text = agent._conn.messages[0]
                    self.assertIn("已进入工具流程", text)
                    self.assertTrue((user_ws.attachments / "v3-sample_scene.moduleAlloc").is_file())
                    self.assertTrue((user_ws.attachments / "v4-sample_scene.moduleAlloc").is_file())
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_textified_task_imports_attachments_then_enters_agent_turn(self) -> None:
        """Regression: textified [文件] + 任务动词必须把 default 里的附件搬到私人空间，
        并把 [资源引用] hint 拼到 user_text，避免 LLM 跨工作区找文件。"""
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "p2p_ou_test",
                        chat_kind="p2p",
                        chat_id="oc_private",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    filename_a = "MemoryReport_MobilePlayer_1.2.0-102-1-v1d0_2026_01_03_04_05_06.csv"
                    filename_b = "MemoryReport_MobilePlayer_1.3.0-103-1-Release_v1d0_2026_01_03_05_06_07.csv"
                    (default_ws.attachments / filename_a).write_text("old", encoding="utf-8")
                    (default_ws.attachments / filename_b).write_text("new", encoding="utf-8")

                    session = _make_test_session_state(session_id="sid", workspace=user_ws, system_prompt=build_system_prompt(user_ws),)
                    seen_user_text: list[str] = []

                    def fake_run_task(task, *, on_event, **kwargs):
                        seen_user_text.append(task.text)
                        on_event(_FinalText(text="已进入工具流程"))
                        return _AgentResult(final_text="已进入工具流程", stop_reason="end_turn")

                    session.session.run_task = fake_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    await agent._prompt_locked(
                        [
                            {
                                "type": "text",
                                "text": (
                                    f"对比下这两个 MemoryReport\n"
                                    f"[文件] {filename_a}\n"
                                    f"[文件] {filename_b}"
                                ),
                            }
                        ],
                        "sid",
                        "mid",
                    )

                    self.assertTrue((user_ws.attachments / filename_a).is_file())
                    self.assertTrue((user_ws.attachments / filename_b).is_file())
                    self.assertFalse((default_ws.attachments / filename_a).exists())
                    self.assertFalse((default_ws.attachments / filename_b).exists())
                    self.assertEqual(len(seen_user_text), 1)
                    hinted = seen_user_text[0]
                    self.assertIn(f"[资源引用: {filename_a}]", hinted)
                    self.assertIn(f"[资源引用: {filename_b}]", hinted)
                    self.assertEqual(len(agent._conn.messages), 1)
                    _sid, reply = agent._conn.messages[0]
                    self.assertIn("已进入工具流程", reply)
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_textified_task_defers_when_default_inbox_is_empty(self) -> None:
        """Regression: textified 附件还没落盘 + 任务动词，必须延迟回复让用户重发，
        而不是带着空 attachments 去跑 LLM。"""
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "p2p_ou_test",
                        chat_kind="p2p",
                        chat_id="oc_private",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    filename = "MemoryReport_pending.csv"

                    session = _make_test_session_state(session_id="sid", workspace=user_ws, system_prompt=build_system_prompt(user_ws),)

                    def fail_run_task(task, **kwargs) -> None:
                        raise AssertionError(f"run_task should not be called: {task}")

                    session.session.run_task = fail_run_task  # type: ignore[method-assign]
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    await agent._prompt_locked(
                        [
                            {
                                "type": "text",
                                "text": f"对比一下\n[文件] {filename}",
                            }
                        ],
                        "sid",
                        "mid",
                    )

                self.assertEqual(len(agent._conn.messages), 1)
                _sid, text = agent._conn.messages[0]
                self.assertIn("已收到附件", text)
                self.assertIn(filename, text)
                self.assertIn("正在保存到你的私人空间", text)
                self.assertIn("保存完成后我会再发一条确认", text)
                self.assertIn("sid", agent._attachment_ack_resource_names)
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_extract_attachment_names_ignores_cc_connect_directory(self) -> None:
        """Regression: ``.cc-connect`` 是 cc-connect 内部目录，不能被识别为附件文件名。"""
        text = (
            "回复 示例用户（示例别名）: \n"
            "[文件] MemoryReport_xxx.csv\n"
            "attachment: file:///srv/workspaces/sample-bot/default/.cc-connect/attachments/MemoryReport_xxx.csv"
        )
        names = extract_attachment_names_from_text(text)
        self.assertIn("MemoryReport_xxx.csv", names)
        self.assertNotIn(".cc-connect", names)
        self.assertNotIn("cc-connect", names)

    def test_debounced_ack_eventually_sends_saved_message_when_file_arrives_late(self) -> None:
        """Regression: cc-connect 写盘比 ACP 进入 prompt 慢时，debounced ack 必须在
        最大轮询窗口内捕捉到文件落盘并送出"文件已保存"回执。"""
        async def run_case() -> None:
            original_delay = acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC
            original_poll = acp_server._ATTACHMENT_ACK_POLL_INTERVAL_SEC
            original_max_wait = acp_server._ATTACHMENT_ACK_MAX_TOTAL_WAIT_SEC
            original_update = acp_server.update_agent_message_text
            acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = 0.02
            acp_server._ATTACHMENT_ACK_POLL_INTERVAL_SEC = 0.02
            acp_server._ATTACHMENT_ACK_MAX_TOTAL_WAIT_SEC = 1.0
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    filename = "MemoryReport_late.csv"

                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    agent._schedule_attachment_ack(
                        session_id="sid",
                        ws=ws,
                        resource_names=[filename],
                    )

                    # 模拟 cc-connect 写盘晚 0.1 秒到位
                    await asyncio.sleep(0.1)
                    (ws.attachments / filename).write_text("frame,cost\n1,2", encoding="utf-8")

                    await asyncio.sleep(0.3)

                self.assertEqual(len(agent._conn.messages), 1)
                _sid, text = agent._conn.messages[0]
                self.assertIn(f"文件已保存到你的私人空间：attachments/{filename}。", text)
                self.assertIn("请告诉我下一步要做什么。", text)
            finally:
                acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = original_delay
                acp_server._ATTACHMENT_ACK_POLL_INTERVAL_SEC = original_poll
                acp_server._ATTACHMENT_ACK_MAX_TOTAL_WAIT_SEC = original_max_wait
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_refresh_system_prompt_sees_files_saved_after_session_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(
                root=Path(tmp),
                chat_kind="p2p",
                chat_id=None,
                user_id="ou_test",
                user_name="tester",
            ).ensure()
            session = _make_test_session_state(session_id="sid", workspace=ws, system_prompt=build_system_prompt(ws),)
            self.assertIn("附件 0 项", session._messages[0]["content"])

            (ws.attachments / "file_a.csv").write_text("a", encoding="utf-8")
            (ws.attachments / "file_b.csv").write_text("b", encoding="utf-8")

            _refresh_session_system_prompt(session)

        self.assertIn("附件 2 项", session._messages[0]["content"])

    def test_attachment_ack_records_context_for_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Workspace(
                root=Path(tmp),
                chat_kind="p2p",
                chat_id=None,
                user_id="ou_test",
                user_name="tester",
            ).ensure()
            session = _make_test_session_state(session_id="sid", workspace=ws, system_prompt=build_system_prompt(ws),)
            (ws.attachments / "file_a.csv").write_text("a", encoding="utf-8")
            ack = format_attachment_ack(ws, ["file_a.csv"])

            session.record_exchange("[资源引用: file_a.csv]", ack)

        self.assertEqual(session._messages[-2]["role"], "user")
        self.assertIn("file_a.csv", session._messages[-2]["content"])
        self.assertEqual(session._messages[-1]["role"], "assistant")
        self.assertIn("文件已保存到你的私人空间：attachments/file_a.csv。", session._messages[-1]["content"])

    def test_text_attachment_prompt_with_missing_file_falls_back_to_receipt_only(self) -> None:
        """Regression: cc-connect 把 file 消息推给 ACP 时若文件还没写到 default
        inbox，``import_transport_attachments`` 会返回空。本路径不再依赖任何
        异步 ack（cc-connect 在 ``end_turn`` 后丢弃 ``session_update``），所以
        只同步发一条 fallback 占位，让用户至少知道消息被接住；最终保存确认
        由用户下条主动问 inventory 来获取。"""
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    session = _make_test_session_state(session_id="sid", workspace=ws, system_prompt=build_system_prompt(ws),)
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    await agent._prompt_locked(
                        [
                            {
                                "type": "text",
                                "text": "示例用户（示例别名）:\n[文件] v3-sample_scene.moduleAlloc",
                            }
                        ],
                        "sid",
                        "mid",
                    )
                self.assertEqual(len(agent._conn.messages), 1)
                _sid, text = agent._conn.messages[0]
                self.assertIn("已收到附件：v3-sample_scene.moduleAlloc", text)
                self.assertIn("正在保存到你的私人空间", text)
                self.assertNotIn("文件已保存到你的私人空间", text)
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_debounced_attachment_ack_coalesces_consecutive_uploads(self) -> None:
        async def run_case() -> None:
            original_delay = acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC
            original_update = acp_server.update_agent_message_text
            acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = 0.01
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    (ws.attachments / "file_a.csv").write_text("a", encoding="utf-8")
                    agent._schedule_attachment_ack(
                        session_id="sid",
                        ws=ws,
                        resource_names=["file_a.csv"],
                    )

                    (ws.attachments / "file_b.csv").write_text("b", encoding="utf-8")
                    agent._schedule_attachment_ack(
                        session_id="sid",
                        ws=ws,
                        resource_names=["file_b.csv"],
                    )

                    await asyncio.sleep(0.05)

                self.assertEqual(len(agent._conn.messages), 1)
                sid, text = agent._conn.messages[0]
                self.assertEqual(sid, "sid")
                self.assertIn("文件已保存到你的私人空间：attachments/file_a.csv、attachments/file_b.csv。", text)
                self.assertIn("你当前累计 2 个已保存文件：", text)
                self.assertIn("- attachments/file_a.csv", text)
                self.assertIn("- attachments/file_b.csv", text)
                self.assertNotIn("做 diff", text)
            finally:
                acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = original_delay
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_cancel_attachment_ack_suppresses_pending_reply(self) -> None:
        async def run_case() -> None:
            original_delay = acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC
            original_update = acp_server.update_agent_message_text
            acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = 0.01
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    ws = Workspace(
                        root=Path(tmp),
                        chat_kind="p2p",
                        chat_id=None,
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {}

                    (ws.attachments / "file_a.csv").write_text("a", encoding="utf-8")
                    agent._schedule_attachment_ack(
                        session_id="sid",
                        ws=ws,
                        resource_names=["file_a.csv"],
                    )
                    agent._cancel_attachment_ack("sid")

                    await asyncio.sleep(0.05)

                self.assertEqual(agent._conn.messages, [])
                self.assertNotIn("sid", agent._attachment_ack_tasks)
                self.assertNotIn("sid", agent._attachment_ack_resource_names)
            finally:
                acp_server._ATTACHMENT_ACK_DEBOUNCE_SEC = original_delay
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())

    def test_workspace_inventory_prompt_imports_pending_default_attachment(self) -> None:
        async def run_case() -> None:
            original_update = acp_server.update_agent_message_text
            acp_server.update_agent_message_text = lambda text: text
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    default_ws = Workspace(
                        root=root / "default",
                        chat_kind=None,
                        chat_id=None,
                    ).ensure()
                    user_ws = Workspace(
                        root=root / "group_oc_team" / "user_ou_test",
                        chat_kind="group",
                        chat_id="oc_team",
                        user_id="ou_test",
                        user_name="tester",
                    ).ensure()
                    (default_ws.attachments / "v3-sample_scene.moduleAlloc").write_text("x", encoding="utf-8")

                    session = _make_test_session_state(session_id="sid", workspace=user_ws, system_prompt=build_system_prompt(user_ws),)
                    agent = AcpChatAgent.__new__(AcpChatAgent)
                    agent._sessions = {"sid": session}
                    agent._conn = _FakeConn()
                    agent._attachment_ack_tasks = {}
                    agent._attachment_ack_resource_names = {"sid": ["v3-sample_scene.moduleAlloc"]}

                    await agent._prompt_locked(
                        [{"type": "text", "text": "我的空间有哪些文件？"}],
                        "sid",
                        "mid",
                    )

                # inventory 查询时，如果还有 pending ack 且文件已落地，
                # 先送一条"文件已保存"回执，再送 inventory 详情。
                self.assertEqual(len(agent._conn.messages), 2)
                _sid_a, ack_text = agent._conn.messages[0]
                self.assertIn("文件已保存到你的私人空间：attachments/v3-sample_scene.moduleAlloc。", ack_text)
                _sid_b, inv_text = agent._conn.messages[1]
                self.assertIn("附件：1 个文件", inv_text)
                self.assertIn("- attachments/v3-sample_scene.moduleAlloc", inv_text)
                self.assertNotIn("全部为空", inv_text)
                self.assertEqual(agent._attachment_ack_resource_names, {})
            finally:
                acp_server.update_agent_message_text = original_update

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
