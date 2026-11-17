import type { ManagementMessage } from "./api";
import { ApplicationRequestError } from "./api";
import { readPendingRequest, savePendingRequest, type PendingApplicationRequest } from "./pendingRequest";

export const DESKTOP_REQUIREMENT_LIMIT = 16000;
export const developmentTextLength = (text: string) => Array.from(text).length;
// Match Python str.strip at the persistence boundary, including NEL and C0
// separators that JavaScript trim does not remove. Interior text stays intact.
export const canonicalDevelopmentText = (text: string) => text.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu, "");

export const DESKTOP_ACTIVE_STATES = new Set(["thinking", "starting", "developing", "verifying", "packaging"]);
export const desktopBusy = (messages: ManagementMessage[]) => messages.some(message => message.blocks_development || (!message.read_only && DESKTOP_ACTIVE_STATES.has(message.state)));
export const readyDesktopBuild = (message: ManagementMessage) => message.state === "ready" && message.build === message.id;
export type PendingDevelopment = PendingApplicationRequest;
export function rejectedDevelopmentRequest(error: unknown, request: PendingDevelopment) {
  // A generic 400 can be a conflict with an existing request. Only an empty
  // canonical requirement is known to be rejected before persistence there.
  return error instanceof ApplicationRequestError && (error.status === 422 ||
    (error.status === 400 && !canonicalDevelopmentText(request.text)));
}
const pendingKey = (application: string) => "agentstrata.desktop-request:" + application;
export function readPendingDevelopment(storage: Pick<Storage, "getItem">, application: string): PendingDevelopment | undefined {
  return readPendingRequest(storage, pendingKey(application));
}
export function savePendingDevelopment(storage: Pick<Storage, "setItem" | "removeItem">, application: string, request?: PendingDevelopment) {
  savePendingRequest(storage, pendingKey(application), request);
}
export function acknowledgedDevelopment(request: PendingDevelopment, messages: ManagementMessage[]) {
  return messages.some(message => message.id === request.id && message.text === canonicalDevelopmentText(request.text));
}
