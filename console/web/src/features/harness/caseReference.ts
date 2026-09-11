import type { FailedCase, SourcePreview } from "./api";

export interface CaseReference { evaluation_id: string; case_ref: string; target_id: string }

export function parseCaseReference(input: string): CaseReference {
  const value = input.trim();
  const format = "请输入完整的单 Case 引用：evalcase:<evaluation_id>/<case_ref>/<target_id>，三个字段分别进行 URL 百分号编码";
  if (!value.startsWith("evalcase:")) throw new Error(format);
  const parts = value.slice("evalcase:".length).split("/");
  if (parts.length !== 3 || parts.some(part => !part)) throw new Error(format);
  let decoded: string[];
  try { decoded = parts.map(part => decodeURIComponent(part)); }
  catch { throw new Error("单 Case 引用包含无效的 URL 百分号编码"); }
  if (decoded.some(part => !part.trim())) throw new Error(format);
  const [evaluation_id, case_ref, target_id] = decoded;
  return { evaluation_id, case_ref, target_id };
}

export function selectedCase(preview: SourcePreview | undefined, reference: CaseReference | undefined): FailedCase | undefined {
  if (!reference || preview?.kind !== "evaluation" || preview.evaluation_id !== reference.evaluation_id) return undefined;
  return preview.failures?.find(item => item.case_ref === reference.case_ref && item.target_id === reference.target_id);
}
