import type { ProvisionField, SetupAction } from "./types";

export function setupActionVerb(action: SetupAction): string {
  return action.default_verb || "start";
}

export function isTerminalSetupAction(action: SetupAction): boolean {
  return action.guided_surface === "terminal";
}

export function terminalQuickstartCommand(botId: string): string {
  return `bash deploy/wsl/quickstart.sh --bot-id ${botId} --resume`;
}

export function editableProvisionFields(fields: ProvisionField[]): ProvisionField[] {
  return fields.filter((field) => !field.host_generated);
}
