import { readFileSync } from "node:fs";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function legalAssets(): Plugin {
  return {
    name: "agentstrata-legal-assets",
    generateBundle(_options, bundle) {
      for (const name of ["LICENSE", "COPYRIGHT", "THIRD_PARTY_NOTICES.md", "THIRD_PARTY_LICENSES.txt"]) {
        this.emitFile({ type: "asset", fileName: name === "THIRD_PARTY_LICENSES.txt" ? "third-party-notices.txt" : name,
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
