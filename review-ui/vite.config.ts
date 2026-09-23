import { defineConfig } from "vitest/config";

export default defineConfig({
  build: {
    outDir: "../core/src/roughcut/review/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: {
    include: ["src/**/*.test.ts"],
  },
});
