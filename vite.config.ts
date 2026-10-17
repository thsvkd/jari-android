import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import { readFileSync } from "node:fs";

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), "VITE_"), ...process.env };
  let provenance: { apiBaseUrl: string; demo: false; nativeVersion: string } | undefined;
  if (mode === "ota") {
    const api = new URL(env.VITE_API_BASE_URL || "invalid:");
    const host = api.hostname.toLowerCase().replace(/\.$/, "");
    const loopback = ["localhost", "0.0.0.0", "[::1]", "[::]", "10.0.2.2"].includes(host) || host.endsWith(".localhost") || host.startsWith("127.") || host.startsWith("[::ffff:");
    if (env.VITE_DEMO_MODE === "true" || api.protocol !== "https:" || loopback || api.username || api.password) throw new Error("OTA 빌드는 데모가 아닌 HTTPS 운영 API 주소가 필요해요.");
    const nativeVersion = readFileSync(new URL("./android/app/build.gradle", import.meta.url), "utf8").match(/versionName "([^"]+)"/)?.[1];
    if (!nativeVersion) throw new Error("OTA 기준 Android 버전을 읽지 못했어요.");
    provenance = { apiBaseUrl: env.VITE_API_BASE_URL!, demo: false, nativeVersion };
  }
  return {
  plugins: provenance ? [{
    name: "ota-build-provenance",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "ota-build.json", source: JSON.stringify(provenance) });
    },
  }] : [],
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
    exclude: ["**/node_modules/**", "**/.git/**", "artifacts/**", "e2e/**", "test-results/**"],
    restoreMocks: true,
    clearMocks: true,
  },
  };
});
