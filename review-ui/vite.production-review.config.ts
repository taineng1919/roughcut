import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["src/production-review-server.smoke.ts"],
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
