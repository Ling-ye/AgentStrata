import { useCallback, useEffect, useRef, useState } from "react";
import { applicationRequestKey, clearAcknowledgedRequest, readPendingRequest, savePendingRequest, type PendingApplicationRequest } from "./pendingRequest";

export function usePendingApplicationRequest(id: string, kind: "qq-configuration" | "qq-development") {
  const key = applicationRequestKey(kind, id);
  const [saved, setSaved] = useState<{ key: string; request?: PendingApplicationRequest }>();
  const [problem, setProblem] = useState<{ key: string; message: string }>();
  const mountedKey = useRef<string>();
  const fail = useCallback((error: unknown) => setProblem({ key, message: error instanceof Error ? error.message : "无法保存原请求，请先检查浏览器存储。" }), [key]);
  useEffect(() => {
    mountedKey.current = key;
    try { setSaved({ key, request: readPendingRequest(sessionStorage, key) }); setProblem(undefined); }
    catch (error) { fail(error); }
    return () => { mountedKey.current = undefined; };
  }, [key, fail]);
  const remember = useCallback((request: PendingApplicationRequest) => {
    try { savePendingRequest(sessionStorage, key, request); setSaved({ key, request }); }
    catch (error) { fail(error); throw error; }
  }, [key, fail]);
  const acknowledge = useCallback((request: PendingApplicationRequest) => {
    try {
      if (!clearAcknowledgedRequest(sessionStorage, key, request)) return false;
      if (mountedKey.current === key) setSaved({ key });
      return true;
    } catch (error) { if (mountedKey.current === key) fail(error); throw error; }
  }, [key, fail]);
  return { pending: saved?.key === key ? saved.request : undefined, restored: saved?.key === key,
    storageError: problem?.key === key ? problem.message : "", remember, acknowledge };
}
