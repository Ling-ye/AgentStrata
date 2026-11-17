/** Exchange the one-time launcher URL secret before mounting any data consumers. */
export function prepareLauncherSession(location: Pick<Location, "hash" | "pathname" | "search">, history: Pick<History, "replaceState">, send: typeof fetch = fetch): (() => Promise<void>) | undefined {
  if (!location.hash.startsWith("#login=")) return undefined;
  const token = new URLSearchParams(location.hash.slice(1)).get("login") || "";
  history.replaceState(null, "", location.pathname + location.search + "#applications");
  let inFlight: Promise<void> | undefined;
  return () => {
    if (!inFlight) inFlight = (async () => {
      try {
        if (!token) throw new Error("missing");
        const response = await send("/api/login", { method: "POST", cache: "no-store", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }) });
        if (!response.ok) throw new Error("denied");
      } catch {
        inFlight = undefined;
        throw new Error("工作台登录未完成。可重试；若启动入口已失效，请重新从本机启动器打开。");
      }
    })();
    return inFlight;
  };
}
