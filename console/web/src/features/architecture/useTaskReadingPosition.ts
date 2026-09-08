import { useLayoutEffect, useRef, type RefObject } from "react";
import { readSessionValue, saveSessionValue, scrollBookmark } from "./taskWorkspaceState";

export function useTaskReadingPosition({ instanceId, runId, active, ready, container, pages, hasMore, fetchingMore, fetchMore }: {
  instanceId: string; runId: string; active: boolean; ready: boolean; container: RefObject<HTMLElement>;
  pages: number; hasMore: boolean; fetchingMore: boolean; fetchMore: () => Promise<unknown>;
}) {
  const latest = useRef({ pages, hasMore, fetchingMore, fetchMore });
  latest.current = { pages, hasMore, fetchingMore, fetchMore };
  useLayoutEffect(() => {
    const element = container.current;
    if (!active || !ready || !runId || !element) return;
    const key = `obs:reading:${instanceId}:${runId}`;
    const saved = scrollBookmark(readSessionValue(key));
    let restoring = true, cancelled = false, requesting = false;
    let frame = 0, settle = 0;
    const cards = () => Array.from(element.querySelectorAll<HTMLElement>("[data-step-key]"));
    const remember = () => {
      if (restoring || cancelled) return;
      const anchor = cards().find((card) => card.getBoundingClientRect().bottom > 0);
      saveSessionValue(key, { top: window.scrollY, anchor: anchor?.dataset.stepKey,
        offset: anchor?.getBoundingClientRect().top ?? 0, pages: latest.current.pages });
    };
    const finish = () => { restoring = false; observer.disconnect(); window.clearTimeout(settle); remember(); };
    const restore = () => {
      if (cancelled || !restoring) return;
      const anchor = saved.anchor ? cards().find((card) => card.dataset.stepKey === saved.anchor) : undefined;
      if (saved.anchor && !anchor && latest.current.hasMore && latest.current.pages < (saved.pages ?? 1)) {
        if (!requesting && !latest.current.fetchingMore) {
          requesting = true;
          void latest.current.fetchMore().then(() => { requesting = false; schedule(); }, finish);
        }
        return;
      }
      window.scrollTo({ top: anchor ? window.scrollY + anchor.getBoundingClientRect().top - saved.offset :
        saved.top || window.scrollY + element.getBoundingClientRect().top - 12, behavior: "instant" });
      if (!settle) settle = window.setTimeout(finish, 1500);
    };
    const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(restore); };
    // Lazy bodies can change height after the first render; stop restoring as soon as the reader interacts.
    const observer = new ResizeObserver(schedule);
    observer.observe(element);
    const intent = () => finish();
    window.addEventListener("scroll", remember, { passive: true });
    window.addEventListener("wheel", intent, { passive: true });
    window.addEventListener("touchstart", intent, { passive: true });
    window.addEventListener("pointerdown", intent);
    window.addEventListener("keydown", intent);
    schedule();
    return () => {
      cancelled = true; observer.disconnect(); cancelAnimationFrame(frame); window.clearTimeout(settle);
      window.removeEventListener("scroll", remember); window.removeEventListener("wheel", intent);
      window.removeEventListener("touchstart", intent); window.removeEventListener("pointerdown", intent);
      window.removeEventListener("keydown", intent);
    };
  }, [instanceId, runId, active, ready, container]);
}
