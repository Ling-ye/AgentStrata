import { createContext, useContext, type ReactNode } from "react";

interface StudioScope {
  id: string;
  apiPrefix: string;
  failedCaseAction?: (evaluationId: string, caseInstanceId: string) => ReactNode;
}
export const StudioContext = createContext<StudioScope>({ id: "", apiPrefix: "/api" });
export const useStudioScope = () => useContext(StudioContext);
