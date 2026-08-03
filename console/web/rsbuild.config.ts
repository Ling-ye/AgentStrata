import { defineConfig } from "@rsbuild/core";
import { pluginReact } from "@rsbuild/plugin-react";

// 开发态：前端跑在 5173，把 /api 代理到后端 8910（含 SSE）。
// 生产态：`npm run build` 产物落 dist/，由 FastAPI 同源挂载，不再需要代理。
export default defineConfig({
  plugins: [pluginReact()],
  source: {
    entry: {
      index: "./src/main.tsx",
    },
  },
  html: {
    template: "./index.html",
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8910",
        changeOrigin: true,
      },
    },
  },
  output: {
    distPath: {
      root: "dist",
    },
  },
});
