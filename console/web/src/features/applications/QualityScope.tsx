import { useMemo, useState, type ReactNode } from "react";
import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { Button } from "@arco-design/web-react";
import { StudioContext } from "../../shared/studioScope";
import { applicationHref } from "./navigation";

export function ApplicationQualityScope({ id, children }: { id: string; children: ReactNode }) {
  const parent = useQueryClient();
  // Each application owns the entire tree of queries, including lazy step bodies and command pages.
  const [cache] = useState(() => new QueryClient({ defaultOptions: parent.getDefaultOptions() }));
  const value = useMemo(
    () => ({
      id,
      apiPrefix: "/api/applications/" + encodeURIComponent(id),
      failedCaseAction: (evaluationId: string, caseInstanceId: string) => (
        <Button
          href={applicationHref(id, "repairs", {
            sourceKind: "evaluation",
            sourceId: caseInstanceId,
            evaluation: evaluationId,
          })}
        >
          发起 Harness 修复
        </Button>
      ),
    }),
    [id],
  );
  return (
    <StudioContext.Provider value={value}>
      <QueryClientProvider client={cache}>{children}</QueryClientProvider>
    </StudioContext.Provider>
  );
}
