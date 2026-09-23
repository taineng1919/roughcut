import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["src/workflow-real-dom.test.ts"],
    testTimeout: 30_000,
  },
});
