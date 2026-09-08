import { useLayoutEffect, useRef, type RefObject } from "react";
import { readSessionValue, saveSessionValue, scrollBookmark } from "./taskWorkspaceState";

export function useTaskReadingPosition({ instanceId, runId, active, ready, container, pages, hasMore, fetchingMore, fetchMore, contentRevision }: {
  instanceId: string; runId: string; active: boolean; ready: boolean; container: RefObject<HTMLElement>;
  pages: number; hasMore: boolean; fetchingMore: boolean; fetchMore: () => Promise<unknown>;
  contentRevision?: unknown;
}) {
  const latest = useRef({ pages, hasMore, fetchingMore, fetchMore });
  const reconcile = useRef<() => void>();
  latest.current = { pages, hasMore, fetchingMore, fetchMore };
  useLayoutEffect(() => {
    const element = container.current;
    if (!active || !ready || !runId || !element) return;
    const key = `obs:reading:${instanceId}:${runId}`;
    let saved = scrollBookmark(readSessionValue(key));
    let restoring = true, cancelled = false, requesting = false;
    let frame = 0, settle = 0, userUntil = 0;
    const cards = () => Array.from(element.querySelectorAll<HTMLElement>("[data-step-key]"));
    const remember = () => {
      if (restoring || cancelled) return;
      const anchor = cards().find((card) => card.getBoundingClientRect().bottom > 0 && card.getBoundingClientRect().top < window.innerHeight);
      saved = { top: window.scrollY, anchor: anchor?.dataset.stepKey,
        offset: anchor?.getBoundingClientRect().top ?? 0, pages: latest.current.pages };
      saveSessionValue(key, saved);
    };
    const finish = () => { restoring = false; window.clearTimeout(settle); settle = 0; remember(); };
    const restore = () => {
      if (cancelled) return;
      if (performance.now() < userUntil) { remember(); return; }
      const anchor = saved.anchor ? cards().find((card) => card.dataset.stepKey === saved.anchor) : undefined;
      if (restoring && latest.current.hasMore && latest.current.pages < (saved.pages ?? 1)) {
        if (!requesting && !latest.current.fetchingMore) {
          requesting = true;
          void latest.current.fetchMore().then(() => { requesting = false; schedule(); }, () => { if (!cancelled) finish(); });
        }
        return;
      }
      if (anchor || restoring) window.scrollTo({ top: anchor ? window.scrollY + anchor.getBoundingClientRect().top - saved.offset :
        saved.top || window.scrollY + element.getBoundingClientRect().top - 12, behavior: "instant" });
      if (restoring && !settle) settle = window.setTimeout(finish, 1500);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(restore); };
    // Preserve the same card through lazy bodies and refreshed ancestry; direct reading gestures take precedence.
    const observer = new ResizeObserver(schedule);
    observer.observe(element);
    const intent = () => { userUntil = performance.now() + 250; finish(); };
    window.addEventListener("scroll", remember, { passive: true });
    window.addEventListener("wheel", intent, { passive: true });
    window.addEventListener("touchstart", intent, { passive: true });
    window.addEventListener("pointerdown", intent);
    window.addEventListener("keydown", intent);
    reconcile.current = schedule;
    schedule();
    return () => {
      cancelled = true; observer.disconnect(); cancelAnimationFrame(frame); window.clearTimeout(settle);
      reconcile.current = undefined;
      window.removeEventListener("scroll", remember); window.removeEventListener("wheel", intent);
      window.removeEventListener("touchstart", intent); window.removeEventListener("pointerdown", intent);
      window.removeEventListener("keydown", intent);
    };
  }, [instanceId, runId, active, ready, container]);
  useLayoutEffect(() => { reconcile.current?.(); }, [contentRevision]);
}
