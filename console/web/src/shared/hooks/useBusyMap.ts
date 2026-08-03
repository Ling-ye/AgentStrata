import { useCallback, useState } from "react";

export function useBusyMap() {
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const setBusyFor = useCallback((id: string, value: boolean) => {
    setBusy((prev) => ({ ...prev, [id]: value }));
  }, []);

  const isBusy = useCallback((id: string) => !!busy[id], [busy]);

  return { busy, isBusy, setBusyFor };
}

