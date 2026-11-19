import { useMemo } from "react";
import { useStudioScope } from "../../shared/studioScope";
import { harnessClient, harnessRequest } from "./api";
import { flowClient } from "./flowModel";
import { improvementClient } from "./improvements";

export function useHarnessClient() {
  const { id, apiPrefix } = useStudioScope();
  return useMemo(() => {
    const harnessBase = apiPrefix + "/harness";
    const request = <T>(path: string, init?: RequestInit) => harnessRequest<T>(path, init, harnessBase);
    return {
      id,
      harnessBase,
      harnessRequest: request,
      harnessApi: harnessClient(request),
      flowApi: flowClient(request),
      improvementApi: improvementClient(request),
    };
  }, [id, apiPrefix]);
}
