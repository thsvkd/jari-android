import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "./",
  server: {
    host: "127.0.0.1",
    port: 4173,
    strictPort: true,
  },
  preview: {
    host: "127.0.0.1",
    port: 4174,
    strictPort: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    // e2e/ 는 Playwright 가 실제 브라우저로 돌려요.
    exclude: ["**/node_modules/**", "**/.git/**", "artifacts/**", "e2e/**"],
    restoreMocks: true,
    clearMocks: true,
  },
});
