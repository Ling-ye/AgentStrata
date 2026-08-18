import { useCallback, useRef, useState } from "react";
import { Message } from "@arco-design/web-react";
import { api } from "../../api";
import type { BotInstance, BotTask } from "../../types";

export type JobsModalSize = { width: number; height: number };

export const DEFAULT_JOBS_MODAL_SIZE: JobsModalSize = { width: 1180, height: 640 };
export const MIN_JOBS_MODAL_SIZE: JobsModalSize = { width: 760, height: 420 };
const JOBS_MODAL_VIEWPORT_PADDING = 48;

export function clampJobsModalSize(size: JobsModalSize): JobsModalSize {
  const maxWidth =
    typeof window === "undefined"
      ? DEFAULT_JOBS_MODAL_SIZE.width
      : Math.max(MIN_JOBS_MODAL_SIZE.width, window.innerWidth - JOBS_MODAL_VIEWPORT_PADDING);
  const maxHeight =
    typeof window === "undefined"
      ? DEFAULT_JOBS_MODAL_SIZE.height
      : Math.max(MIN_JOBS_MODAL_SIZE.height, window.innerHeight - JOBS_MODAL_VIEWPORT_PADDING);
  return {
    width: Math.min(Math.max(size.width, MIN_JOBS_MODAL_SIZE.width), maxWidth),
    height: Math.min(Math.max(size.height, MIN_JOBS_MODAL_SIZE.height), maxHeight),
  };
}

export function useJobsModal() {
  const [open, setOpen] = useState(false);
  const [bot, setBot] = useState<BotInstance | null>(null);
  const [jobs, setJobs] = useState<BotTask[]>([]);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [workspaceRoot, setWorkspaceRoot] = useState("");
  const [workspaceExists, setWorkspaceExists] = useState<boolean | null>(null);
  const requestIdRef = useRef(0);
  const inFlightRef = useRef<{ key: string; promise: Promise<void> } | null>(null);

  const load = useCallback((targetBot: BotInstance, opts?: { clear?: boolean }): Promise<void> => {
    if (opts?.clear) {
      setJobs([]);
      setUpdatedAt(null);
    }
    const requestKey = targetBot.instance_id;
    if (inFlightRef.current?.key === requestKey) return inFlightRef.current.promise;
    const requestId = ++requestIdRef.current;
    const request = (async () => {
      setLoading(true);
      setError(null);
      try {
        const resp = await api.tasks(targetBot.instance_id);
        if (requestIdRef.current !== requestId) return;
        const sorted = [...resp.tasks].sort(
          (a, b) => (Number(b.sort_time) || 0) - (Number(a.sort_time) || 0),
        );
        setJobs(sorted);
        setWorkspaceRoot(resp.workspace_root);
        setWorkspaceExists(resp.workspace_exists);
        setUpdatedAt(Date.now());
      } catch (e) {
        if (requestIdRef.current !== requestId) return;
        const message = e instanceof Error ? e.message : String(e);
        setError(message);
        Message.error(message);
      } finally {
        if (requestIdRef.current === requestId) {
          setLoading(false);
          inFlightRef.current = null;
        }
      }
    })();
    inFlightRef.current = { key: requestKey, promise: request };
    return request;
  }, []);

  const show = useCallback(
    (targetBot: BotInstance) => {
      setBot(targetBot);
      setOpen(true);
      setUpdatedAt(null);
      setError(null);
      setWorkspaceRoot("");
      setWorkspaceExists(null);
      void load(targetBot, { clear: true });
    },
    [load],
  );

  const close = useCallback(() => setOpen(false), []);

  return {
    open,
    bot,
    jobs,
    updatedAt,
    loading,
    error,
    workspaceRoot,
    workspaceExists,
    load,
    show,
    close,
  };
}
