export type WindowRole = 'settings' | 'client' | 'manager';
export interface Caller { senderId: number; frameIsMain: boolean; url: string }
export interface WindowGrant { id: number; url: string; role: WindowRole }

export function sameHttpOrigin(actual: string, expected: string): boolean {
  try {
    const url = new URL(actual);
    return url.protocol === 'http:' && url.origin === new URL(expected).origin;
  } catch { return false; }
}

export function allowed(caller: Caller, grant: WindowGrant | undefined, roles: WindowRole[]): boolean {
  if (!grant || caller.senderId !== grant.id || !caller.frameIsMain || !roles.includes(grant.role)) return false;
  try {
    const actual = new URL(caller.url), expected = new URL(grant.url);
    return grant.role === 'settings' ? actual.href === expected.href : sameHttpOrigin(caller.url, grant.url);
  } catch { return false; }
}
