export function pointerChild(path: string, key: string | number) {
  return `${path}/${String(key).replace(/~/g, "~0").replace(/\//g, "~1")}`;
}
export function valueType(value: unknown) {
  return value === undefined ? "missing" : value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
}
export function valueSummary(value: unknown): string {
  if (value === undefined) return "字段缺失";
  if (value === null) return "null";
  if (Array.isArray(value)) return value.length ? `Array(${value.length})` : "[]";
  if (typeof value === "object") return Object.keys(value).length ? `Object(${Object.keys(value).length})` : "{}";
  if (value === "") return '""';
  return String(value);
}
export interface DataMatch { path: string; key: string; value: unknown }
/** Search only the supplied, already-loaded JSON. Iterative traversal also handles deep payloads. */
export function searchData(value: unknown, query: string, limit = 100): { matches: DataMatch[]; more: boolean } {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return { matches: [], more: false };
  const pending: DataMatch[] = [{ path: "", key: "$", value }], matches: DataMatch[] = [];
  while (pending.length) {
    const entry = pending.pop()!;
    const container = entry.value !== null && typeof entry.value === "object";
    if (entry.key.toLocaleLowerCase().includes(needle) || !container && valueSummary(entry.value).toLocaleLowerCase().includes(needle)) {
      if (matches.length === limit) return { matches, more: true };
      matches.push(entry);
    }
    if (container) {
      const children = Object.entries(entry.value as object);
      for (let index = children.length - 1; index >= 0; index--) {
        const [key, child] = children[index];
        pending.push({ path: pointerChild(entry.path, key), key, value: child });
      }
    }
  }
  return { matches, more: false };
}

export async function copyDataText(text: string) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
  const element = document.createElement("textarea");
  const previous = document.activeElement;
  element.value = text; element.style.position = "fixed"; element.style.opacity = "0";
  document.body.appendChild(element); element.select();
  try { if (!document.execCommand("copy")) throw new Error("复制失败"); }
  finally { element.remove(); if (previous instanceof HTMLElement) previous.focus(); }
}
