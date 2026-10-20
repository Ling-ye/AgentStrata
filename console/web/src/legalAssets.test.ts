import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { createServer, type ViteDevServer } from "vite";

describe("development legal assets", () => {
  let server: ViteDevServer;
  let origin: string;

  beforeAll(async () => {
    server = await createServer({
      root: fileURLToPath(new URL("..", import.meta.url)),
      configFile: fileURLToPath(new URL("../vite.config.ts", import.meta.url)),
      configLoader: "runner",
      optimizeDeps: { noDiscovery: true, include: [] },
      server: { host: "127.0.0.1", port: 0, strictPort: false },
    });
    await server.listen();
    const address = server.httpServer!.address();
    if (!address || typeof address === "string") throw new Error("Vite did not bind a TCP port");
    origin = `http://127.0.0.1:${address.port}`;
  });

  afterAll(async () => { await server?.close(); });

  it.each([
    ["LICENSE", "LICENSE"],
    ["COPYRIGHT", "COPYRIGHT"],
    ["THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"],
    ["third-party-notices.txt", "THIRD_PARTY_LICENSES.txt"],
  ])("serves /%s as the complete legal file", async (path, name) => {
    const response = await fetch(`${origin}/${path}`);
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/plain; charset=utf-8");
    expect(await response.text()).toBe(readFileSync(new URL(`../../../${name}`, import.meta.url), "utf-8"));
  });

  it("supports query strings and HEAD without the SPA fallback", async () => {
    const response = await fetch(`${origin}/LICENSE?download=1`, { method: "HEAD" });
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/plain; charset=utf-8");
    expect(Number(response.headers.get("content-length"))).toBeGreaterThan(30_000);
    expect(await response.text()).toBe("");
  });

  it("matches legal paths exactly", async () => {
    const path = "LICENSE" + "@" + "notes.md";
    const response = await fetch(`${origin}/${path}`);
    expect(response.headers.get("content-type")).toContain("text/html");
    expect(await response.text()).toContain('<div id="root">');
  });
});
