import { afterEach, expect, it } from "vitest";
import { mkdtempSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const temporary = [];
afterEach(() => temporary.splice(0).forEach((dir) => rmSync(dir, { recursive: true, force: true })));
function build(env) {
  const out = mkdtempSync(join(tmpdir(), "jari-ota-vite-")); temporary.push(out);
  const result = spawnSync(process.execPath, [resolve("node_modules/vite/bin/vite.js"), "build", "--mode", "ota", "--outDir", out], {
    encoding: "utf8", env: { ...process.env, VITE_API_BASE_URL: "https://api.example.com", VITE_DEMO_MODE: "false", ...env },
  });
  return { ...result, out };
}

it("records the API, demo flag and native version in a dedicated OTA build", () => {
  const result = build({});
  expect(result.status, result.stderr).toBe(0);
  const provenance = JSON.parse(readFileSync(join(result.out, "ota-build.json"), "utf8"));
  const gradle = readFileSync("android/app/build.gradle", "utf8");
  expect(provenance).toEqual({ apiBaseUrl: "https://api.example.com", demo: false, nativeVersion: gradle.match(/versionName "([^"]+)"/)[1] });
});

it.each([{ VITE_DEMO_MODE: "true" }, { VITE_API_BASE_URL: "http://127.0.0.1:18281" }, { VITE_API_BASE_URL: "https://localhost" }, { VITE_API_BASE_URL: "https://[::ffff:127.0.0.1]" }, { VITE_API_BASE_URL: "https://localhost." }])("refuses OTA builds with development-only configuration: %j", (env) => {
  const result = build(env);
  expect(result.status).not.toBe(0);
  expect(existsSync(join(result.out, "ota-build.json"))).toBe(false);
});
