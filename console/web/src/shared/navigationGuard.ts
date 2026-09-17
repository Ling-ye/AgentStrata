import { createContext, useCallback, useEffect, useRef, useState } from "react";

interface NavigationGuard { shouldBlock: (hash: string) => boolean; confirm: () => Promise<boolean> }
type RegisterGuard = (guard: NavigationGuard) => () => void;
export const NavigationGuardContext = createContext<RegisterGuard>(() => () => {});

export function leavesBotInstance(hash: string, instanceId: string) {
  const [page, query] = hash.split("?");
  const next = new URLSearchParams(query).get("instance");
  return page !== "#bots" || !!next && next !== instanceId;
}

// One guard for the currently mounted editor. Capture hash changes before page listeners
// so back/forward, sidebar navigation and direct links all obey the same decision.
export function useGuardedHash() {
  const [hash, setHash] = useState(window.location.hash);
  const accepted = useRef(hash);
  const guard = useRef<NavigationGuard | null>(null);
  const confirming = useRef(false);
  const approved = useRef<string | null>(null);
  const registerGuard = useCallback<RegisterGuard>((value) => {
    guard.current = value;
    return () => { if (guard.current === value) guard.current = null; };
  }, []);
  useEffect(() => {
    const update = (event: HashChangeEvent) => {
      const next = window.location.hash;
      if (approved.current === next) approved.current = null;
      else if (confirming.current || guard.current?.shouldBlock(next)) {
        event.stopImmediatePropagation();
        if (!confirming.current) accepted.current = new URL(event.oldURL).hash;
        window.history.replaceState(null, "", window.location.pathname + window.location.search + accepted.current);
        if (!confirming.current && guard.current) {
          confirming.current = true;
          void guard.current.confirm().then((leave) => {
            confirming.current = false;
            if (leave) { approved.current = next; window.location.hash = next; }
          });
        }
        return;
      }
      accepted.current = next;
      setHash(next);
    };
    window.addEventListener("hashchange", update, true);
    return () => window.removeEventListener("hashchange", update, true);
  }, []);
  const navigate = useCallback((next: string) => { window.location.hash = next; }, []);
  return { hash, navigate, registerGuard };
}
