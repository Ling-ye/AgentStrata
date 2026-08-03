---
id: multimodal-image-io
type: architecture
status: implemented
created: 2026-07-29
---

# Multimodal Image Input and Output

## Summary

AgentStrata already lets the main Agent download public images and deliver workspace files
through the platform-neutral `file_sender` hook. QQ image output is transported directly through
OneBot, while Feishu uses its existing file sender. The missing half is image understanding:
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
  5 MiB per image, and 20 MiB per turn, and stored under the current per-user workspace. Uploaded
  image files pass the same validation after the existing transport import step. Supported vision
  formats are JPEG, PNG, GIF, and WebP.
- The middleware passes only validated workspace-local `ResourceRef` values to `AgentTask`.
  An image-only turn is stored safely and receives a deterministic acknowledgement without
  invoking a model. The next ordinary text instruction in the same session that enters the model
  consumes those pending images exactly once. An image and natural-language instruction received
  in the same turn are forwarded for immediate analysis.
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
- Rollout is capability-gated. Disabling `chat.image_inputs` rolls back image forwarding without
  changing attachment storage, text turns, file delivery, or backend selection.

## Acceptance

- An enabled instance can receive a valid PNG, JPEG, GIF, or WebP ACP image block and the selected
  backend receives the image together with the user's caption.
- A supported uploaded image file follows the existing private-workspace import boundary and is
  presented to the backend as an image rather than merely as a filename.
- Image-only turns are stored with a deterministic acknowledgement and are consumed exactly once
  by the next ordinary text instruction that enters the model; image-plus-text turns are analyzed
  immediately.
- Invalid base64, spoofed MIME/signature pairs, unsupported formats, excessive image counts, and
  oversized payloads fail closed with a concise user-facing error and do not invoke a model.
- Native/LangGraph transcripts contain no image base64 payloads.
- Codex new and resumed turns attach every validated image with `--image`.
- QQ refuses a filename-only image spoof before opening the OneBot delivery path, while valid
  image output and non-image file fallback continue to work.
- Existing text-only, generic attachment, image-search download, and file-delivery behavior
  remains compatible.

## Verification

Run:

```bash
python3 scripts/check_sdd_specs.py
.venv/bin/python -m pytest tests/unit/test_multimodal_image_io.py tests/unit/test_multimodal_backends.py tests/unit/test_multimodal_turn_orchestration.py tests/unit/test_acp_image_capabilities.py tests/unit/test_qq_sender.py tests/unit/test_main_agent_backend_unification.py -q --basetemp=/tmp/chatcopilot-pytest-image-io
.venv/bin/python -m pytest tests/integration/test_acp_attachment_gate.py -q --basetemp=/tmp/chatcopilot-pytest-image-attachment
.venv/bin/python -m chatcopilot botspec validate bots/lingye-copilot-qq/bot.yaml
.venv/bin/python -m compileall -q src bots tests
git diff --check
```
