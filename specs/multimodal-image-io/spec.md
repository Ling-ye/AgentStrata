---
id: multimodal-image-io
type: architecture
status: implemented
created: 2026-07-29
---

# Multimodal Image Input and Output

## Summary

AgentStrata lets the main Agent download public images and deliver workspace files through the
platform-neutral `file_sender` hook. QQ image output is transported directly through OneBot,
while Feishu uses its existing file sender. Public-URL delivery is also available as one explicit
`send_image_urls_to_user` action so the model does not have to infer a fragile two-tool sequence;
a Markdown image or link is never treated as a delivery receipt. The remaining multimodal half is
image understanding:
ACP accepts `ImageContentBlock`, but the current prompt pipeline reduces a turn to text and file
names, so neither the Native/LangGraph chat-completions path nor the Codex backend receives the
image.

This change makes a validated image a first-class `AgentTask` resource and completes an
opt-in, end-to-end path from an ACP image block or uploaded image file to a vision-capable
backend. It also hardens QQ image output by validating the bytes as well as the filename before
calling OneBot.

The scope is image understanding plus reliable delivery of images produced by search or tools.
Binding a paid text-to-image provider is explicitly out of scope: that requires a separate
provider, credential, moderation, quota, and cost contract and must not be smuggled into the
existing `llm.chat / llm.research / llm.code` three-slot design.

## Design

- `chat.image_inputs` is an explicit BotSpec runtime feature. An instance only materializes and
  forwards images when this feature is enabled; unsupported instances return a deterministic
  explanation instead of sending an empty prompt to an LLM. ACP initialization mirrors the same
  switch through `promptCapabilities.image`, so clients negotiate the actual runtime capability.
- The shared `ResourceRef` contract carries optional `media_type`, `size_bytes`, and `sha256`
  metadata. The resource remains a `kind: file`; media metadata refines the file rather than
  adding a parallel image-only resource hierarchy.
- ACP inline images are strict-base64 decoded, signature-checked, limited to at most four images,
  5 MiB per image, and 20 MiB per turn, and stored under the current conversation workspace. QQ
  private chats retain a per-user workspace; an admitted QQ group stores images in the current
  group's dedicated shared root under the [`qq-group-shared-conversation-context`](../qq-group-shared-conversation-context/spec.md)
  contract and records the originating actor with the accepted turn. Uploaded image files that are
  already bound to the current conversation workspace pass the same validation. The cc-connect
  static `default/.cc-connect/attachments` inbox has no chat/message identity, so a QQ shared group
  fails closed instead of importing an image from that ambiguous legacy location. Supported vision
  formats are JPEG, PNG, GIF, and WebP.
- The middleware passes only validated workspace-local `ResourceRef` values to `AgentTask`.
  An image-only turn is stored safely and receives a deterministic acknowledgement without
  invoking a model. The next ordinary text instruction in the same session that enters the model
  consumes those pending images exactly once. An image and natural-language instruction received
  in the same turn are forwarded for immediate analysis.
- If a private/actor-scoped transport attachment is referenced before a safely bound file is visible,
  the accepted user turn and deterministic saving response are recorded synchronously; its debounced
  acknowledgement is delivery-only. A QQ shared-group basename-only attachment is instead rejected
  synchronously because neither the legacy inbox nor an existing same-named shared file proves its
  chat/message origin; that rejection is recorded once and schedules no delayed acknowledgement.
- Native and LangGraph sessions keep local-image descriptors in message history. The shared LLM
  request boundary revalidates the file identity and expands descriptors to OpenAI-compatible
  `image_url` data URLs only for the outbound request. Base64 image bytes are therefore not kept
  in session state or transcripts.
- The Codex backend maps validated image resources to `codex exec --image` for both new and
  resumed native sessions. Textual resource framing remains in the prompt so filenames and
  provenance are visible without exposing raw bytes.
- Image output remains an explicit main-Agent action through `send_files_to_user`; this preserves
  the existing user-facing tool permission boundary and avoids duplicate automatic sends. QQ
  continues to use OneBot for supported image files, but checks the content signature against
  the suffix before encoding it.
- When the user asks to send public image URLs, `send_image_urls_to_user` combines the existing
  downloader and `file_sender` hook. It checks that a sender exists before any download, accepts at
  most five URLs at 5 MiB each, sends all valid images in one delivery, and reports sanitized
  partial failures without exposing URL credentials or query parameters. It follows at most five
  redirects; every hop resolves and connects only to validated public addresses. The tool reports
  success only after a complete platform receipt. An uncertain delivery is not retried
  automatically and cannot be described as sent.
- When system DNS returns only transparent-proxy addresses from the RFC 2544 benchmark Fake-IP
  block, the
  downloader does not connect to that reserved range. It resolves the hostname through a public
  DoH endpoint reached by fixed public bootstrap addresses with TLS hostname validation, validates
  the returned A/AAAA records with the same public-address policy, and pins the image connection to
  those independently resolved addresses. Other non-public or mixed DNS answers, unavailable DoH,
  and malformed responses continue to fail closed.
- The `workspace.read_write` capability policy tells every main backend to prefer the combined
  action for public image URLs and to keep `send_files_to_user` for existing workspace files.
  Both tools remain hidden from subagents because they deliver directly to the user. No manual
  approval, domain allowlist, or per-image confirmation is added.
- Rollout is capability-gated. Disabling `chat.image_inputs` rolls back image forwarding without
  changing attachment storage, text turns, file delivery, or backend selection.

## Acceptance

- An enabled instance can receive a valid PNG, JPEG, GIF, or WebP ACP image block and the selected
  backend receives the image together with the user's caption.
- A supported image already bound to the current conversation workspace is presented to the backend
  as an image rather than merely as a filename. A QQ shared group never claims an image from the
  unbound static cc-connect inbox by basename alone.
- Image-only turns are stored with a deterministic acknowledgement and are consumed exactly once
  by the next ordinary text instruction that enters the model; image-plus-text turns are analyzed
  immediately.
- An actor-scoped pending attachment turn is recorded exactly once before the immediate saving
  response, and its later acknowledgement cannot append under a different actor. A QQ shared-group
  unbound attachment receives one deterministic rejection and no delayed acknowledgement.
- Invalid base64, spoofed MIME/signature pairs, unsupported formats, excessive image counts, and
  oversized payloads fail closed with a concise user-facing error and do not invoke a model.
- Native/LangGraph transcripts contain no image base64 payloads.
- Codex new and resumed turns attach every validated image with `--image`.
- QQ refuses a filename-only image spoof before opening the OneBot delivery path, while valid
  image output and non-image file fallback continue to work.
- A public image URL request can download and send one or more valid images with one platform
  action. Partial download failures do not block valid peers, all-invalid input performs no send,
  private or redirected-private targets fail closed, and only the successful platform receipt can
  support an “images sent” claim.
- Existing text-only, generic attachment, image-search download, and file-delivery behavior
  remains compatible.

## Verification

The original commands below cover multimodal validation and conversation-local storage. The QQ
shared-group static-inbox rejection and delivery-only acknowledgement extension is additionally
verified by the focused suite in
[`qq-group-shared-conversation-context`](../qq-group-shared-conversation-context/spec.md); it does
not claim a real QQ two-account ingress E2E.

Run:

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_multimodal_image_io.py tests/unit/test_multimodal_backends.py tests/unit/test_multimodal_turn_orchestration.py tests/unit/test_acp_image_capabilities.py tests/unit/test_image_download_tool.py tests/unit/test_image_url_delivery_tool.py tests/unit/test_qq_sender.py tests/unit/test_main_agent_backend_unification.py -q --basetemp=/tmp/chatcopilot-pytest-image-io
.venv/bin/python -m pytest tests/integration/test_acp_attachment_gate.py -q --basetemp=/tmp/chatcopilot-pytest-image-attachment
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
.venv/bin/python -m compileall -q src bots tests
git diff --check
```
