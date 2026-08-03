import { useEffect } from "react";

export function usePolling(
  enabled: boolean,
  callback: () => void | Promise<void>,
  intervalMs: number,
) {
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    const tick = () => {
      if (!cancelled) void callback();
    };
    const timer = window.setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [callback, enabled, intervalMs]);
}

