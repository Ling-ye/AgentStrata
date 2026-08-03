"""platforms.router 工厂分发与能力位单测。"""
from __future__ import annotations

import unittest

from chatcopilot.platforms import router
from chatcopilot.platforms.router import UnsupportedPlatformError


class PlatformRouterTests(unittest.TestCase):
    """覆盖 router 对 feishu / qq 两条线的分发与能力位声明。"""

    def test_supported_platform_types_includes_known(self) -> None:
        types = router.supported_platform_types()
        self.assertIn("feishu", types)
        self.assertIn("qq", types)

    def test_is_supported_case_insensitive(self) -> None:
        self.assertTrue(router.is_supported("feishu"))
        self.assertTrue(router.is_supported("QQ"))
        self.assertTrue(router.is_supported(" feishu "))
        self.assertFalse(router.is_supported("twitter"))
        self.assertFalse(router.is_supported(""))

    def test_unknown_platform_type_raises(self) -> None:
        with self.assertRaises(UnsupportedPlatformError):
            router.get_sender("twitter")
        with self.assertRaises(UnsupportedPlatformError):
            router.get_notifier("twitter")

    def test_sender_modules_expose_send_via_cc_connect(self) -> None:
        for ptype in ("feishu", "qq"):
            sender = router.get_sender(ptype)
            self.assertTrue(
                hasattr(sender, "send_via_cc_connect"),
                msg=f"platform {ptype} sender 缺少 send_via_cc_connect",
            )
            self.assertTrue(
                hasattr(sender, "resolve_sendable_paths"),
                msg=f"platform {ptype} sender 缺少 resolve_sendable_paths",
            )

    def test_notifier_modules_expose_send_text_to_workspace(self) -> None:
        for ptype in ("feishu", "qq"):
            notifier = router.get_notifier(ptype)
            self.assertTrue(hasattr(notifier, "send_text_to_workspace"))
            self.assertTrue(hasattr(notifier, "resolve_delivery_target"))

    def test_capability_flags_match_design(self) -> None:
        # 飞书：完整启用
        self.assertTrue(router.supports_role_matrix("feishu"))
        self.assertTrue(router.supports_user_files_pipeline("feishu"))
        self.assertTrue(router.supports_background_jobs("feishu"))
        # QQ 第一阶段：纯问答骨架
        self.assertFalse(router.supports_role_matrix("qq"))
        self.assertFalse(router.supports_user_files_pipeline("qq"))
        self.assertTrue(router.supports_background_jobs("qq"))

    def test_capability_flags_unknown_platform_raises(self) -> None:
        with self.assertRaises(UnsupportedPlatformError):
            router.supports_role_matrix("twitter")


if __name__ == "__main__":
    unittest.main()
