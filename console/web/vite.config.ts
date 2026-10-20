import { readFileSync } from "node:fs";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const legalFiles = new Map([
  ["LICENSE", "LICENSE"],
  ["COPYRIGHT", "COPYRIGHT"],
  ["THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"],
  ["third-party-notices.txt", "THIRD_PARTY_LICENSES.txt"],
]);

function legalAssets(): Plugin {
  return {
    name: "agentstrata-legal-assets",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.method !== "GET" && request.method !== "HEAD") return next();
        const path = new URL(request.url ?? "/", "http://localhost").pathname;
        const name = legalFiles.get(path.slice(1));
        if (!name) return next();
        try {
          const source = readFileSync(new URL(`../../${name}`, import.meta.url));
          response.setHeader("Content-Type", "text/plain; charset=utf-8");
          response.setHeader("Content-Length", source.length);
          response.setHeader("Cache-Control", "no-cache");
          response.end(request.method === "HEAD" ? undefined : source);
        } catch (error) {
          next(error);
        }
      });
    },
    generateBundle(_options, bundle) {
      for (const [fileName, name] of legalFiles) {
        this.emitFile({ type: "asset", fileName,
          source: readFileSync(new URL(`../../${name}`, import.meta.url), "utf-8") });
      }
      const packages = new Set<string>();
      for (const output of Object.values(bundle)) {
        if (output.type !== "chunk") continue;
        for (const module of Object.keys(output.modules)) {
          for (const match of module.matchAll(/\/node_modules\/((?:@[^/]+\/)?[^/]+)/g)) packages.add(match[1]);
        }
      }
      this.emitFile({ type: "asset", fileName: "third-party-packages.json",
        source: JSON.stringify([...packages].sort(), null, 2) + "\n" });
    },
  };
}

export default defineConfig({
  plugins: [react(), legalAssets()],
  server: { port: 5173, proxy: { "/api": { target: "http://127.0.0.1:8910", changeOrigin: false } } },
  build: {
    outDir: "dist",
    rollupOptions: { output: { banner: "/*! AgentStrata · AGPL-3.0-only · Copyright (C) 2026 Lingye. Third-party notices: third-party-notices.txt */" } },
  },
});
