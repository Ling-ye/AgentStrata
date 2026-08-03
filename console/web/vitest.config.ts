import { defineConfig } from "vitest/config";

const runtime = globalThis as typeof globalThis & {
  process?: {
    platform: string;
    env: Record<string, string | undefined>;
  };
};
if (runtime.process && runtime.process.platform !== "win32") {
  runtime.process.env.TMPDIR = "/tmp";
  runtime.process.env.TEMP = "/tmp";
  runtime.process.env.TMP = "/tmp";
}

export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
