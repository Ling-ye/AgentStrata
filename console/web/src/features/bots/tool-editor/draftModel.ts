import type { BotToolConfig } from "../../../types";

export interface DraftState { saved: BotToolConfig | null; draft: BotToolConfig | null }
export type DraftAction =
  | { type: "receive"; config: BotToolConfig }
  | { type: "change"; update: (config: BotToolConfig) => BotToolConfig }
  | { type: "discard" }
  | { type: "saved"; submitted: BotToolConfig; config: BotToolConfig };

export function draftIsDirty(state: DraftState) {
  return !!state.draft && JSON.stringify(state.draft) !== JSON.stringify(state.saved);
}
export function draftReducer(state: DraftState, action: DraftAction): DraftState {
  switch (action.type) {
    case "receive": return { saved: action.config, draft: draftIsDirty(state) ? state.draft : action.config };
    case "change": return state.draft ? { ...state, draft: action.update(state.draft) } : state;
    case "discard": return { ...state, draft: state.saved };
    case "saved": return { saved: action.config, draft: state.draft === action.submitted ? action.config : state.draft };
  }
}
