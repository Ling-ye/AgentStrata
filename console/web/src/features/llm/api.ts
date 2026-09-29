export interface ModelConnection {
  kind: "codex" | "openai_responses" | "openai_compatible";
  base_url?: string;
  auth: { mode: "chatgpt"; profile: "main" | "worker" } | { mode: "api_key"; key_env: string };
  timeout?: number;
  env_file?: string;
  codex_bin_env?: string;
  credential_root_env?: string;
}
export interface ModelProfile { connection: string; model: string; reasoning_effort?: string }
export interface ModelConfiguration {
  connections: Record<string, ModelConnection>;
  profiles: Record<string, ModelProfile>;
  bindings: Record<string, string>;
  revision: string;
  source?: "saved" | "defaults" | "empty";
  resolved: Record<string, { profile: string; model: string; reasoning_effort: string | null }>;
}
export interface ModelCatalog {
  connection: string;
  source: string;
  fetched_at: string | null;
  error: string | null;
  models: { id: string; name: string; reasoning_efforts: string[] | null;
    default_reasoning_effort: string | null; context_window: number | null; hidden: boolean }[];
}
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api/llm${path}`, { cache: "no-store", ...options,
    headers: { "Content-Type": "application/json", ...options?.headers } });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : JSON.stringify(value.detail ?? value));
  return value;
}
export const llmApi = {
  config: (signal?: AbortSignal) => request<ModelConfiguration>("/config", { signal }),
  save: ({ connections, profiles, bindings, revision }: ModelConfiguration) => request<ModelConfiguration>("/config", {
    method: "PUT", body: JSON.stringify({ connections, profiles, bindings, revision }) }),
  refresh: (connection: string, draft?: ModelConnection, signal?: AbortSignal) => request<ModelCatalog>("/catalog/refresh", {
    method: "POST", body: JSON.stringify({ connection, draft }), signal }),
};
export function profileOptions(config: ModelConfiguration | undefined, worker = false) {
  return Object.entries(config?.profiles ?? {}).filter(([, profile]) => {
    const connection = config?.connections[profile.connection];
    return !!connection && (!worker || (connection.kind === "codex" && connection.auth.mode === "chatgpt" && connection.auth.profile === "worker"));
  }).map(([value, profile]) => ({ value, label: `${value} · ${profile.model}${profile.reasoning_effort ? ` / ${profile.reasoning_effort}` : ""}` }));
}
