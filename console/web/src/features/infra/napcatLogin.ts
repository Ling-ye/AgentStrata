const NAPCAT_WEBUI_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

export function safeNapcatLoginUrl(value: string | null): string | null {
  if (!value) return null;

  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }

  if (
    url.protocol !== "http:"
    || !NAPCAT_WEBUI_HOSTS.has(url.hostname.toLowerCase())
    || !url.port
    || url.username
    || url.password
    || url.pathname !== "/webui"
    || url.search
    || url.hash
  ) {
    return null;
  }
  return url.toString();
}

type WindowOpener = (url: string, target: string, features: string) => unknown;

export function openNapcatLoginPage(
  value: string | null,
  opener?: WindowOpener,
): boolean {
  const url = safeNapcatLoginUrl(value);
  if (!url) return false;
  const openWindow = opener ?? ((...args) => window.open(...args));
  openWindow(url, "_blank", "noopener,noreferrer");
  return true;
}

type TokenLoader = () => Promise<{ token: string }>;
type ClipboardWriter = (value: string) => Promise<void>;

export async function copyNapcatWebUiToken(
  loadToken: TokenLoader,
  writer?: ClipboardWriter,
): Promise<void> {
  const result = await loadToken();
  if (!result.token || result.token.length > 512 || /\s/.test(result.token)) {
    throw new Error("NapCat 返回了无效的 WebUI Token");
  }
  const writeText = writer ?? ((value) => navigator.clipboard.writeText(value));
  await writeText(result.token);
}
