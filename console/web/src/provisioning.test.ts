import { describe, expect, it } from "vitest";

import {
  editableProvisionFields,
  isTerminalSetupAction,
  setupActionVerb,
  terminalQuickstartCommand,
} from "./provisioning";

describe("provisioning helpers", () => {
  it("uses the schema default verb instead of assuming start", () => {
    expect(setupActionVerb({ id: "qq-gateway", label: "QQ", default_verb: "bootstrap" }))
      .toBe("bootstrap");
    expect(setupActionVerb({ id: "legacy", label: "Legacy" })).toBe("start");
  });

  it("routes terminal-guided actions to the resumable quickstart", () => {
    expect(isTerminalSetupAction({
      id: "qq-gateway",
      label: "QQ",
      guided_surface: "terminal",
    })).toBe(true);
    expect(terminalQuickstartCommand("my-assistant-qq"))
      .toBe("bash deploy/wsl/quickstart.sh --bot-id my-assistant-qq --resume");
  });

  it("keeps host-generated fields in the schema but out of the editable form", () => {
    const fields = [
      {
        field: "chat_api_key",
        env_key: "CHATCOPILOT_CHAT_API_KEY",
        label: "LLM API Key",
        group: "llm",
        required: true,
        secret: true,
        configured: false,
      },
      {
        field: "qq_access_token",
        env_key: "QQ_ACCESS_TOKEN",
        label: "OneBot Access Token",
        group: "platform",
        required: true,
        secret: true,
        configured: true,
        host_generated: true,
      },
      {
        field: "qq_account",
        env_key: "QQ_ACCOUNT",
        label: "机器人 QQ 号",
        group: "platform",
        required: true,
        secret: false,
        configured: false,
      },
    ];

    expect(editableProvisionFields(fields).map((field) => field.field)).toEqual([
      "chat_api_key",
      "qq_account",
    ]);
    expect(editableProvisionFields(fields.filter((field) => field.field !== "qq_access_token")))
      .toEqual(fields.filter((field) => field.field !== "qq_access_token"));
  });
});
