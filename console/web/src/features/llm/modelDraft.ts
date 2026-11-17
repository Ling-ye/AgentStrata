import type { ModelConfiguration } from "./api";

// Later edits remain a separate draft based on the acknowledged save revision.
export function modelDraftAfterSave(current: ModelConfiguration, saved: ModelConfiguration, editedWhileSaving: boolean): ModelConfiguration {
  return editedWhileSaving ? { ...current, revision: saved.revision, source: saved.source } : saved;
}

export function leavesModelsPage(hash: string): boolean {
  return hash.split("?")[0] !== "#models";
}
