from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, mock

from chatcopilot.application.resources import (
    FetchedResource,
    ResourceMaterializationError,
    ResourceMaterializationLimits,
    ResourceMaterializationService,
)
from chatcopilot.contracts.gateway import (
    CanonicalInboundEvent,
    ChannelAccountRef,
    ConversationRef,
    MessageSegment,
    ResourceTicket,
    SenderClaim,
    TransportEvidence,
)
from chatcopilot.contracts.workspace import (
    WORKSPACE_SCOPE_ACTOR,
    WORKSPACE_SCOPE_GROUP_SHARED,
    WorkspaceView,
)


ACCOUNT = ChannelAccountRef(channel="qq", account_id="10001")
CONVERSATION = ConversationRef(kind="group", conversation_id="30003")
ACTOR = "20002"
MESSAGE_ID = "message-1"
EVENT_ID = "event-1"
DATA = b"validated attachment bytes"
SHA256 = hashlib.sha256(DATA).hexdigest()


class _Fetcher:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[ResourceTicket, int]] = []

    async def fetch(self, ticket: ResourceTicket, *, max_bytes: int) -> FetchedResource:
        self.calls.append((ticket, max_bytes))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result  # type: ignore[return-value]


def _ticket(**changes: object) -> ResourceTicket:
    values: dict[str, object] = {
        "ticket_id": "ticket-1",
        "account": ACCOUNT,
        "conversation": CONVERSATION,
        "sender_id": ACTOR,
        "event_id": EVENT_ID,
        "message_id": MESSAGE_ID,
        "kind": "image",
        "name": "photo.png",
        "media_type": "image/png",
        "size_bytes": len(DATA),
        "sha256": SHA256,
        "expires_at": 200.0,
        "provider_ref": {"opaque": "provider-resource-1"},
    }
    values.update(changes)
    return ResourceTicket(**values)  # type: ignore[arg-type]


def _event(
    *,
    tickets: tuple[ResourceTicket, ...] | None = None,
    segments: tuple[MessageSegment, ...] | None = None,
) -> CanonicalInboundEvent:
    ticket = _ticket()
    return CanonicalInboundEvent(
        evidence=TransportEvidence(
            account=ACCOUNT,
            conversation=CONVERSATION,
            sender=SenderClaim(sender_id=ACTOR, display_name="Actor"),
            event_id=EVENT_ID,
            message_id=MESSAGE_ID,
            connection_generation="connection-1",
            frame_sha256="a" * 64,
            observed_at=100.0,
        ),
        segments=(
            segments
            if segments is not None
            else (MessageSegment(kind="image", resource_ticket_id=ticket.ticket_id),)
        ),
        resource_tickets=tickets if tickets is not None else (ticket,),
    )


class ResourceMaterializationTests(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory(prefix="agentstrata-resource-test-")
        root = Path(self._temporary.name) / "actor-workspace"
        root.mkdir(mode=0o700)
        self.workspace = WorkspaceView(
            root=root,
            chat_kind="group",
            chat_id=CONVERSATION.conversation_id,
            user_id=ACTOR,
            scope=WORKSPACE_SCOPE_GROUP_SHARED,
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _service(
        self,
        fetcher: _Fetcher,
        *,
        limits: ResourceMaterializationLimits | None = None,
    ) -> ResourceMaterializationService:
        return ResourceMaterializationService(fetcher, limits=limits)

    async def test_success_atomically_materializes_private_single_link_file(self) -> None:
        fetcher = _Fetcher(
            FetchedResource(data=DATA, name="photo.png", media_type="image/png")
        )

        references = await self._service(fetcher).materialize(
            event=_event(),
            actor_id=ACTOR,
            workspace=self.workspace,
            now=150.0,
        )

        self.assertEqual(len(references), 1)
        reference = references[0]
        path = Path(reference.path)
        self.assertEqual(reference.name, "photo.png")
        self.assertEqual(reference.kind, "file")
        self.assertEqual(reference.media_type, "image/png")
        self.assertEqual(reference.size_bytes, len(DATA))
        self.assertEqual(reference.sha256, SHA256)
        self.assertEqual(path.read_bytes(), DATA)
        self.assertEqual(path.relative_to(self.workspace.root).parts[:2], ("attachments", "inbound"))
        current = path.stat()
        self.assertTrue(stat.S_ISREG(current.st_mode))
        self.assertEqual(current.st_nlink, 1)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(current.st_mode), 0o600)
        self.assertFalse(any(item.name.startswith(".tmp-") for item in path.parent.iterdir()))
        self.assertEqual(fetcher.calls[0][1], 25 * 1024 * 1024)

    async def test_cross_actor_is_rejected_before_fetch(self) -> None:
        fetcher = _Fetcher(FetchedResource(data=DATA))

        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(fetcher).materialize(
                event=_event(),
                actor_id="29999",
                workspace=self.workspace,
                now=150.0,
            )

        self.assertEqual(caught.exception.code, "resource_actor_mismatch")
        self.assertEqual(fetcher.calls, [])
        self.assertFalse((self.workspace.root / "attachments").exists())

    async def test_group_workspace_requires_shared_scope_exact_actor_and_group(self) -> None:
        cases = (
            (
                replace(self.workspace, scope=WORKSPACE_SCOPE_ACTOR),
                "resource_workspace_scope_mismatch",
            ),
            (
                replace(self.workspace, user_id="29999"),
                "resource_workspace_actor_mismatch",
            ),
            (
                replace(self.workspace, chat_id="39999"),
                "resource_workspace_conversation_mismatch",
            ),
        )
        for workspace, expected_code in cases:
            fetcher = _Fetcher(FetchedResource(data=DATA))
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(ResourceMaterializationError) as caught:
                    await self._service(fetcher).materialize(
                        event=_event(),
                        actor_id=ACTOR,
                        workspace=workspace,
                        now=150.0,
                    )
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(fetcher.calls, [])

    async def test_p2p_workspace_requires_actor_scope_and_exact_actor(self) -> None:
        conversation = ConversationRef(kind="p2p", conversation_id=ACTOR)
        ticket = replace(_ticket(), conversation=conversation)
        event = replace(
            _event(tickets=(ticket,)),
            evidence=replace(_event().evidence, conversation=conversation),
        )
        actor_workspace = replace(
            self.workspace,
            chat_kind="p2p",
            chat_id=ACTOR,
            scope=WORKSPACE_SCOPE_ACTOR,
        )
        fetcher = _Fetcher(
            FetchedResource(data=DATA, name="photo.png", media_type="image/png")
        )

        references = await self._service(fetcher).materialize(
            event=event,
            actor_id=ACTOR,
            workspace=actor_workspace,
            now=150.0,
        )

        self.assertEqual(len(references), 1)
        shared_workspace = replace(
            actor_workspace,
            root=Path(self._temporary.name) / "unused-shared",
            scope=WORKSPACE_SCOPE_GROUP_SHARED,
        )
        shared_workspace.root.mkdir(mode=0o700)
        rejected_fetcher = _Fetcher(FetchedResource(data=DATA))
        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(rejected_fetcher).materialize(
                event=event,
                actor_id=ACTOR,
                workspace=shared_workspace,
                now=150.0,
            )
        self.assertEqual(caught.exception.code, "resource_workspace_scope_mismatch")
        self.assertEqual(rejected_fetcher.calls, [])

        wrong_actor = replace(
            actor_workspace,
            root=Path(self._temporary.name) / "unused-actor",
            user_id="29999",
        )
        wrong_actor.root.mkdir(mode=0o700)
        rejected_fetcher = _Fetcher(FetchedResource(data=DATA))
        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(rejected_fetcher).materialize(
                event=event,
                actor_id=ACTOR,
                workspace=wrong_actor,
                now=150.0,
            )
        self.assertEqual(caught.exception.code, "resource_workspace_actor_mismatch")
        self.assertEqual(rejected_fetcher.calls, [])

    async def test_expired_ticket_is_rejected_before_fetch(self) -> None:
        ticket = _ticket(expires_at=149.0)
        fetcher = _Fetcher(FetchedResource(data=DATA))

        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(fetcher).materialize(
                event=_event(tickets=(ticket,)),
                actor_id=ACTOR,
                workspace=self.workspace,
                now=150.0,
            )

        self.assertEqual(caught.exception.code, "resource_ticket_expired")
        self.assertEqual(fetcher.calls, [])

    async def test_ticket_must_match_event_account_conversation_message_and_actor(self) -> None:
        drifted = (
            (_ticket(account=ChannelAccountRef(channel="qq", account_id="99999")), "account"),
            (_ticket(conversation=ConversationRef(kind="group", conversation_id="39999")), "conversation"),
            (_ticket(sender_id="29999"), "actor"),
            (_ticket(event_id="event-other"), "event"),
            (_ticket(message_id="message-other"), "message"),
        )
        for ticket, _label in drifted:
            fetcher = _Fetcher(FetchedResource(data=DATA))
            with self.assertRaises(ResourceMaterializationError) as caught:
                await self._service(fetcher).materialize(
                    event=_event(tickets=(ticket,)),
                    actor_id=ACTOR,
                    workspace=self.workspace,
                    now=150.0,
                )
            self.assertEqual(caught.exception.code, "resource_ticket_binding_mismatch")
            self.assertEqual(fetcher.calls, [])

    async def test_extra_missing_duplicate_and_kind_drift_tickets_are_rejected(self) -> None:
        ticket = _ticket()
        cases = (
            (
                _event(
                    tickets=(ticket,),
                    segments=(MessageSegment(kind="text", text="no resource"),),
                ),
                "resource_ticket_extra",
            ),
            (
                _event(
                    tickets=(),
                    segments=(MessageSegment(kind="image", resource_ticket_id="missing"),),
                ),
                "resource_ticket_missing",
            ),
            (_event(tickets=(ticket, ticket)), "resource_ticket_duplicate"),
            (
                _event(
                    tickets=(ticket,),
                    segments=(
                        MessageSegment(kind="image", resource_ticket_id=ticket.ticket_id),
                        MessageSegment(kind="image", resource_ticket_id=ticket.ticket_id),
                    ),
                ),
                "resource_ticket_reference_duplicate",
            ),
            (
                _event(
                    tickets=(replace(ticket, kind="file"),),
                    segments=(
                        MessageSegment(kind="image", resource_ticket_id=ticket.ticket_id),
                    ),
                ),
                "resource_ticket_kind_mismatch",
            ),
            (
                _event(
                    tickets=(ticket,),
                    segments=(MessageSegment(kind="image"),),
                ),
                "resource_ticket_reference_missing",
            ),
        )
        for event, expected_code in cases:
            fetcher = _Fetcher(FetchedResource(data=DATA))
            with self.assertRaises(ResourceMaterializationError) as caught:
                await self._service(fetcher).materialize(
                    event=event,
                    actor_id=ACTOR,
                    workspace=self.workspace,
                    now=150.0,
                )
            self.assertEqual(caught.exception.code, expected_code)
            self.assertEqual(fetcher.calls, [])

    async def test_size_hash_and_media_drift_leave_no_files(self) -> None:
        cases = (
            FetchedResource(data=DATA + b"x", name="photo.png", media_type="image/png"),
            FetchedResource(data=b"x" * len(DATA), name="photo.png", media_type="image/png"),
            FetchedResource(data=DATA, name="photo.png", media_type="image/jpeg"),
        )
        expected_codes = (
            "resource_size_mismatch",
            "resource_sha256_mismatch",
            "resource_media_type_mismatch",
        )
        for fetched, expected_code in zip(cases, expected_codes, strict=True):
            fetcher = _Fetcher(fetched)
            with self.assertRaises(ResourceMaterializationError) as caught:
                await self._service(fetcher).materialize(
                    event=_event(),
                    actor_id=ACTOR,
                    workspace=self.workspace,
                    now=150.0,
                )
            self.assertEqual(caught.exception.code, expected_code)
            self.assertFalse((self.workspace.root / "attachments").exists())

    async def test_path_traversal_name_is_rejected_without_escape(self) -> None:
        ticket = _ticket(name=None)
        fetcher = _Fetcher(
            FetchedResource(data=DATA, name="../escape.bin", media_type="image/png")
        )

        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(fetcher).materialize(
                event=_event(tickets=(ticket,)),
                actor_id=ACTOR,
                workspace=self.workspace,
                now=150.0,
            )

        self.assertEqual(caught.exception.code, "resource_name_unsafe")
        self.assertFalse((Path(self._temporary.name) / "escape.bin").exists())
        self.assertFalse((self.workspace.root / "attachments").exists())

    async def test_symlinked_attachment_directory_is_rejected_before_fetch(self) -> None:
        outside = Path(self._temporary.name) / "outside"
        outside.mkdir()
        (self.workspace.root / "attachments").symlink_to(outside, target_is_directory=True)
        fetcher = _Fetcher(FetchedResource(data=DATA))

        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(fetcher).materialize(
                event=_event(),
                actor_id=ACTOR,
                workspace=self.workspace,
                now=150.0,
            )

        self.assertEqual(caught.exception.code, "resource_workspace_unsafe")
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(tuple(outside.iterdir()), ())

    async def test_fetch_failure_leaves_no_attachment_directory_or_partial_file(self) -> None:
        fetcher = _Fetcher(RuntimeError("provider unavailable with private detail"))

        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(fetcher).materialize(
                event=_event(),
                actor_id=ACTOR,
                workspace=self.workspace,
                now=150.0,
            )

        self.assertEqual(caught.exception.code, "resource_fetch_failed")
        self.assertNotIn("private detail", str(caught.exception))
        self.assertFalse((self.workspace.root / "attachments").exists())

    async def test_fetch_port_cannot_return_a_path_or_url_instead_of_bytes(self) -> None:
        fetcher = _Fetcher(str(self.workspace.root / "provider-file"))

        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(fetcher).materialize(
                event=_event(),
                actor_id=ACTOR,
                workspace=self.workspace,
                now=150.0,
            )

        self.assertEqual(caught.exception.code, "resource_fetch_result_invalid")
        self.assertFalse((self.workspace.root / "attachments").exists())

    async def test_configured_per_file_and_total_limits_are_enforced(self) -> None:
        limits = ResourceMaterializationLimits(max_files=1, max_file_bytes=8, max_total_bytes=8)
        fetcher = _Fetcher(FetchedResource(data=DATA))

        with self.assertRaises(ResourceMaterializationError) as caught:
            await self._service(fetcher, limits=limits).materialize(
                event=_event(),
                actor_id=ACTOR,
                workspace=self.workspace,
                now=150.0,
            )

        self.assertEqual(caught.exception.code, "resource_declared_size_limit_exceeded")
        self.assertEqual(fetcher.calls, [])

    async def test_atomic_publish_failure_rolls_back_temporary_and_final_files(self) -> None:
        fetcher = _Fetcher(
            FetchedResource(data=DATA, name="photo.png", media_type="image/png")
        )
        with mock.patch(
            "chatcopilot.application.resources.service.os.link",
            side_effect=OSError("injected publish failure"),
        ):
            with self.assertRaises(ResourceMaterializationError) as caught:
                await self._service(fetcher).materialize(
                    event=_event(),
                    actor_id=ACTOR,
                    workspace=self.workspace,
                    now=150.0,
                )

        self.assertEqual(caught.exception.code, "resource_storage_failed")
        attachments = self.workspace.root / "attachments"
        self.assertFalse(attachments.exists() and any(attachments.rglob("*")))


class ResourceLimitValidationTests(IsolatedAsyncioTestCase):
    async def test_limits_require_positive_plain_integers(self) -> None:
        for changes in (
            {"max_files": 0},
            {"max_file_bytes": True},
            {"max_total_bytes": -1},
        ):
            with self.assertRaises(ValueError):
                ResourceMaterializationLimits(**changes)
