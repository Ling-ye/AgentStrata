from __future__ import annotations

import base64
import hashlib
import tempfile
import unittest
from pathlib import Path

from acp.schema import ImageContentBlock

from chatcopilot.core.image_content import ImageContentError
from chatcopilot.core.workspace_runtime import Workspace
from chatcopilot.middleware.acp.image_pipeline import (
    image_resource_ref,
    materialize_inline_images,
)

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class MultimodalImageIoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(
            root=Path(self._tmp.name),
            chat_kind="p2p",
            chat_id="test-chat",
            user_id="test-user",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inline_image_rejects_invalid_base64_without_writing(self) -> None:
        block = ImageContentBlock(
            type="image",
            data="not-valid-base64%%%",
            mimeType="image/png",
        )

        with self.assertRaises(ImageContentError):
            materialize_inline_images([block], self.workspace)

        self.assertFalse((self.workspace.attachments / "images").exists())

    def test_inline_image_rejects_mime_magic_mismatch_without_writing(self) -> None:
        block = ImageContentBlock(
            type="image",
            data=base64.b64encode(_PNG_BYTES).decode("ascii"),
            mimeType="image/jpeg",
        )

        with self.assertRaises(ImageContentError):
            materialize_inline_images([block], self.workspace)

        self.assertFalse((self.workspace.attachments / "images").exists())

    def test_inline_image_is_persisted_as_metadata_rich_resource_ref(self) -> None:
        block = ImageContentBlock(
            type="image",
            data=base64.b64encode(_PNG_BYTES).decode("ascii"),
            mimeType="image/png",
        )

        resources = materialize_inline_images([block], self.workspace)

        self.assertEqual(len(resources), 1)
        resource = resources[0]
        expected_sha256 = hashlib.sha256(_PNG_BYTES).hexdigest()
        image_path = Path(resource.path)
        self.assertEqual(
            image_path.parent,
            (self.workspace.attachments / "images").resolve(),
        )
        self.assertEqual(image_path.name, f"{expected_sha256}.png")
        self.assertEqual(image_path.read_bytes(), _PNG_BYTES)
        self.assertEqual(resource.name, image_path.name)
        self.assertEqual(resource.kind, "file")
        self.assertEqual(resource.media_type, "image/png")
        self.assertEqual(resource.size_bytes, len(_PNG_BYTES))
        self.assertEqual(resource.sha256, expected_sha256)
        self.assertTrue(self.workspace.is_inside(image_path))

    def test_imported_workspace_image_becomes_resource_ref(self) -> None:
        self.workspace.attachments.mkdir(parents=True)
        imported = self.workspace.attachments / "uploaded.png"
        imported.write_bytes(_PNG_BYTES)

        resource = image_resource_ref(imported, self.workspace)

        self.assertEqual(resource.name, "uploaded.png")
        self.assertEqual(Path(resource.path), imported.resolve())
        self.assertEqual(resource.media_type, "image/png")
        self.assertEqual(resource.size_bytes, len(_PNG_BYTES))
        self.assertEqual(resource.sha256, hashlib.sha256(_PNG_BYTES).hexdigest())

    def test_imported_image_rejects_extension_magic_mismatch(self) -> None:
        self.workspace.attachments.mkdir(parents=True)
        spoofed = self.workspace.attachments / "spoofed.jpg"
        spoofed.write_bytes(_PNG_BYTES)

        with self.assertRaises(ImageContentError):
            image_resource_ref(spoofed, self.workspace)

    def test_imported_image_rejects_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as outside:
            candidate = Path(outside) / "outside.png"
            candidate.write_bytes(_PNG_BYTES)

            with self.assertRaises(ImageContentError):
                image_resource_ref(candidate, self.workspace)


if __name__ == "__main__":
    unittest.main()
