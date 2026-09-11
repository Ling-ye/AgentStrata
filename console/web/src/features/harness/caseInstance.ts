import type { CaseInstance, SourcePreview } from "./api";

export function caseInstanceId(input: string): string {
  const value = input.trim();
  if (!/^case-[0-9a-f]{32}$/.test(value)) throw new Error("请输入完整的 Case 实例 ID（case- 开头），从测评结果中复制");
  return value;
}

export function selectedInstance(preview: SourcePreview | undefined, id: string): CaseInstance | undefined {
  const instance = preview?.case_instance;
  return preview?.kind === "evaluation" && instance?.case_instance_id === id
    && instance.evaluation_id === preview.evaluation_id ? instance : undefined;
}
