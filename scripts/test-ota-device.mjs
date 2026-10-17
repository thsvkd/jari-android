#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { generateKeyPairSync } from "node:crypto";
import {
  chmodSync,
  copyFileSync,
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmdirSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:https";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { chromium } from "@playwright/test";

const ROOT = resolve(import.meta.dirname, "..");
const APP = "dev.thsvkd.jari";
const ACTIVITY = "com.jari.app.MainActivity";
const HTTPS_PORT = 23_443;
const CDP_PORT = 9_378;
const NATIVE_VERSION = readFileSync(join(ROOT, "android/app/build.gradle"), "utf8")
  .match(/versionName\s+"([^"]+)"/)?.[1];
const API_ORIGIN = "https://ota-test.invalid";
const SESSION_SENTINEL = "ota-device-smoke-session-not-a-real-credential";
const NETWORK_CONFIG = join(ROOT, "android/app/src/debug/res/xml/debug_network_security_config.xml");
const TEST_CA = join(ROOT, "android/app/src/debug/res/raw/jari_ota_test_ca.pem");
const DIST = join(ROOT, "dist");
const ANDROID_ASSETS = join(ROOT, "android/app/src/main/assets");

if (process.platform === "darwin") {
  if (!process.env.JAVA_HOME && existsSync("/opt/homebrew/opt/openjdk@21")) process.env.JAVA_HOME = "/opt/homebrew/opt/openjdk@21";
  if (!process.env.ANDROID_HOME && existsSync("/opt/homebrew/share/android-commandlinetools")) process.env.ANDROID_HOME = "/opt/homebrew/share/android-commandlinetools";
  if (process.env.JAVA_HOME) process.env.PATH = `${process.env.JAVA_HOME}/bin:${process.env.PATH}`;
}

if (!NATIVE_VERSION) throw new Error("android versionName을 읽지 못했어요.");

function run(command, args, { cwd = ROOT, env = {}, capture = false, allowFailure = false, timeout = 120_000 } = {}) {
  const printable = [command, ...args].map((part) => part.includes(" ") ? JSON.stringify(part) : part).join(" ");
  console.log(`\n$ ${printable}`);
  const result = spawnSync(command, args, {
    cwd,
    env: { ...process.env, ...env },
    encoding: "utf8",
    stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit",
    timeout,
  });
  if (result.error) throw result.error;
  if (result.status !== 0 && !allowFailure) {
    throw new Error(`${printable} 실패 (${result.status})\n${result.stdout || ""}${result.stderr || ""}`.trim());
  }
  return result;
}

function delay(milliseconds) {
  return new Promise((complete) => setTimeout(complete, milliseconds));
}

function requireEmulator() {
  const serial = process.env.ANDROID_SERIAL;
  if (!serial?.startsWith("emulator-")) {
    throw new Error("이 검증은 앱 데이터와 설치 앱을 바꿔요. ANDROID_SERIAL=emulator-… 를 명시해 주세요.");
  }
  const devices = run("adb", ["devices"], { capture: true }).stdout
    .split(/\r?\n/)
    .slice(1)
    .filter((line) => /\tdevice$/.test(line))
    .map((line) => line.split("\t")[0]);
  if (!devices.includes(serial)) throw new Error(`연결된 에뮬레이터가 아니에요: ${serial}`);
  return serial;
}

function snapshotPath(path, backupRoot) {
  const backup = join(backupRoot, basename(path));
  const existed = existsSync(path);
  if (existed) {
    mkdirSync(backupRoot, { recursive: true });
    cpSync(path, backup, { recursive: true, force: false, errorOnExist: true });
  }
  return () => {
    rmSync(path, { recursive: true, force: true });
    if (existed) {
      mkdirSync(dirname(path), { recursive: true });
      cpSync(backup, path, { recursive: true, force: false, errorOnExist: true });
    }
  };
}

function createSigningKey(temporary) {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const privatePath = join(temporary, "ota-private.pem");
  writeFileSync(privatePath, privateKey.export({ type: "pkcs8", format: "pem" }), { mode: 0o600 });
  chmodSync(privatePath, 0o600);
  return {
    privatePath,
    publicKey: publicKey.export({ type: "spki", format: "der" }).toString("base64"),
  };
}

function createLocalCertificate(temporary) {
  const key = join(temporary, "localhost-key.pem");
  const certificate = join(temporary, "localhost-cert.pem");
  run("openssl", [
    "req", "-x509", "-newkey", "rsa:2048", "-nodes",
    "-keyout", key,
    "-out", certificate,
    "-days", "1",
    "-subj", "/CN=localhost",
    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
    "-addext", "keyUsage=digitalSignature,keyEncipherment",
    "-addext", "extendedKeyUsage=serverAuth",
  ]);
  chmodSync(key, 0o600);
  return { key, certificate };
}

function installDebugCa(certificate) {
  mkdirSync(dirname(TEST_CA), { recursive: true });
  copyFileSync(certificate, TEST_CA);
  writeFileSync(NETWORK_CONFIG, `<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="false">
        <trust-anchors>
            <certificates src="@raw/jari_ota_test_ca" />
        </trust-anchors>
    </base-config>
</network-security-config>
`);
}

function createOtaDist(source, destination) {
  cpSync(source, destination, { recursive: true });
  writeFileSync(join(destination, "ota-build.json"), JSON.stringify({
    apiBaseUrl: API_ORIGIN,
    demo: false,
    nativeVersion: NATIVE_VERSION,
  }));
}

function buildBundle({ dist, out, version, sequence, key }) {
  run(process.execPath, [
    join(ROOT, "scripts/build-ota.mjs"),
    "--version", version,
    "--sequence", String(sequence),
    "--native-version", NATIVE_VERSION,
    "--url", `https://localhost:${HTTPS_PORT}/${version}/bundle.zip`,
    "--key", key,
    "--dist", dist,
    "--out", out,
  ]);
}

function tamperManifest(manifestPath) {
  const envelope = JSON.parse(readFileSync(manifestPath, "utf8"));
  const payload = JSON.parse(Buffer.from(envelope.payload, "base64").toString("utf8"));
  payload.version = `${payload.version}-tampered`;
  return Buffer.from(JSON.stringify({
    ...envelope,
    payload: Buffer.from(JSON.stringify(payload), "utf8").toString("base64"),
  }));
}

async function startUpdateServer({ key, certificate, releases }) {
  let manifest = releases.success.manifest;
  const files = new Map([
    [`/${releases.success.version}/bundle.zip`, releases.success.bundle],
    [`/${releases.broken.version}/bundle.zip`, releases.broken.bundle],
    [`/${releases.cold.version}/bundle.zip`, releases.cold.bundle],
  ]);
  const server = createServer({
    key: readFileSync(key),
    cert: readFileSync(certificate),
  }, (request, response) => {
    const path = new URL(request.url, `https://localhost:${HTTPS_PORT}`).pathname;
    const body = path === "/manifest.json" ? manifest : files.get(path);
    if (!body) {
      response.writeHead(404, { "Content-Type": "text/plain", "Cache-Control": "no-store" });
      response.end("not found");
      return;
    }
    response.writeHead(200, {
      "Content-Type": path.endsWith(".zip") ? "application/zip" : "application/json",
      "Content-Length": body.length,
      "Cache-Control": "no-store",
    });
    response.end(body);
  });
  await new Promise((complete, reject) => {
    server.once("error", reject);
    server.listen(HTTPS_PORT, "127.0.0.1", complete);
  });
  return {
    use(name) {
      manifest = releases[name].manifest;
    },
    close: () => new Promise((complete, reject) => server.close((error) => error ? reject(error) : complete())),
  };
}

function adb(serial, args, options = {}) {
  return run("adb", ["-s", serial, ...args], options);
}

async function waitFor(description, probe, timeoutMs = 60_000, intervalMs = 500) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const result = await probe();
      if (result) return result;
    } catch (error) {
      lastError = error;
    }
    await delay(intervalMs);
  }
  throw new Error(`${description} 대기 시간 초과${lastError ? `: ${lastError.message}` : ""}`);
}

async function connectWebView(serial) {
  const pid = await waitFor("앱 프로세스", async () => {
    const result = adb(serial, ["shell", "pidof", APP], { capture: true, allowFailure: true });
    return result.status === 0 && result.stdout.trim() || undefined;
  });
  const socket = await waitFor("WebView 디버깅 소켓", async () => {
    const output = adb(serial, ["shell", "cat", "/proc/net/unix"], { capture: true }).stdout;
    return output.split(/\r?\n/).map((line) => line.trim().split(/\s+/).pop())
      .find((name) => name === `@webview_devtools_remote_${pid}`);
  });
  adb(serial, ["forward", "--remove", `tcp:${CDP_PORT}`], { allowFailure: true, capture: true });
  adb(serial, ["forward", `tcp:${CDP_PORT}`, `localabstract:${socket.slice(1)}`]);
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP_PORT}`, { noDefaults: true });
  const page = await waitFor("앱 WebView 페이지", async () => browser.contexts()[0]?.pages()[0]);
  await waitFor("Capacitor AppUpdates 플러그인", async () => page.evaluate(() => Boolean(
    window.Capacitor?.Plugins?.AppUpdates && window.Capacitor?.Plugins?.SecureSession,
  )));
  return { browser, page };
}

async function plugin(page, name, method, argument) {
  return page.evaluate(async ({ name, method, argument }) => {
    const target = window.Capacitor?.Plugins?.[name];
    if (!target || typeof target[method] !== "function") throw new Error(`${name}.${method} is unavailable`);
    return argument === undefined ? target[method]() : target[method](argument);
  }, { name, method, argument });
}

async function expectRejected(label, operation) {
  try {
    await operation();
  } catch {
    console.log(`✓ ${label}`);
    return;
  }
  throw new Error(`${label}: 거부되지 않았어요.`);
}

function assertEqual(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label}: 기대 ${JSON.stringify(expected)}, 실제 ${JSON.stringify(actual)}`);
  console.log(`✓ ${label}`);
}

async function applyAndWaitForVersion(connection, version, timeoutMs = 60_000) {
  // setServerBasePath resolves the old JS call before native reloads the URL.
  // Observe the new document before querying its bridge, rather than racing a
  // promise against destruction of the old execution context.
  const loaded = connection.page.waitForEvent("domcontentloaded", { timeout: timeoutMs });
  void loaded.catch(() => undefined);
  await plugin(connection.page, "AppUpdates", "apply").catch((error) => {
    if (!/Execution context was destroyed|Target page, context or browser has been closed/.test(error.message)) throw error;
  });
  await loaded;
  return waitFor(`${version} 적용`, async () => {
    const status = await plugin(connection.page, "AppUpdates", "getStatus");
    return status.currentVersion === version ? status : undefined;
  }, timeoutMs);
}

async function restartApp(serial, connection) {
  await connection.browser.close().catch(() => undefined);
  adb(serial, ["shell", "am", "force-stop", APP]);
  adb(serial, ["shell", "am", "start", "-n", `${APP}/${ACTIVITY}`]);
  return connectWebView(serial);
}

async function main() {
  const serial = requireEmulator();
  const temporary = mkdtempSync(join(tmpdir(), "jari-ota-device-"));
  const backup = join(temporary, "backup");
  mkdirSync(backup);
  const restore = [
    snapshotPath(DIST, join(backup, "dist")),
    snapshotPath(ANDROID_ASSETS, join(backup, "android-assets")),
    snapshotPath(NETWORK_CONFIG, join(backup, "network-config")),
    snapshotPath(TEST_CA, join(backup, "test-ca")),
  ].reverse();
  let server;
  let connection;
  try {
    const signing = createSigningKey(temporary);
    const tls = createLocalCertificate(temporary);
    installDebugCa(tls.certificate);

    run("npm", ["run", "build"], {
      env: { VITE_API_BASE_URL: API_ORIGIN, VITE_DEMO_MODE: "false" },
      timeout: 10 * 60_000,
    });
    const successVersion = `${NATIVE_VERSION}-device-success`;
    const brokenVersion = `${NATIVE_VERSION}-device-broken`;
    const coldVersion = `${NATIVE_VERSION}-device-cold`;
    const successDist = join(temporary, "success-dist");
    const brokenDist = join(temporary, "broken-dist");
    createOtaDist(DIST, successDist);
    mkdirSync(brokenDist);
    // Keep Capacitor's native injection point so the probe can observe native
    // state after navigation, while deliberately never acknowledging readiness.
    writeFileSync(join(brokenDist, "index.html"), "<!doctype html><html><head><meta charset='utf-8'><title>OTA rollback probe</title></head><body>Rollback probe</body></html>");
    writeFileSync(join(brokenDist, "ota-build.json"), JSON.stringify({ apiBaseUrl: API_ORIGIN, demo: false, nativeVersion: NATIVE_VERSION }));

    const successOut = join(temporary, "success-release");
    const brokenOut = join(temporary, "broken-release");
    const coldOut = join(temporary, "cold-release");
    buildBundle({ dist: successDist, out: successOut, version: successVersion, sequence: 1, key: signing.privatePath });
    buildBundle({ dist: brokenDist, out: brokenOut, version: brokenVersion, sequence: 2, key: signing.privatePath });
    buildBundle({ dist: brokenDist, out: coldOut, version: coldVersion, sequence: 3, key: signing.privatePath });
    const releases = {
      success: {
        version: successVersion,
        manifest: readFileSync(join(successOut, "manifest.json")),
        bundle: readFileSync(join(successOut, "bundle.zip")),
      },
      malicious: {
        manifest: tamperManifest(join(successOut, "manifest.json")),
      },
      broken: {
        version: brokenVersion,
        manifest: readFileSync(join(brokenOut, "manifest.json")),
        bundle: readFileSync(join(brokenOut, "bundle.zip")),
      },
      cold: {
        version: coldVersion,
        manifest: readFileSync(join(coldOut, "manifest.json")),
        bundle: readFileSync(join(coldOut, "bundle.zip")),
      },
    };
    server = await startUpdateServer({ key: tls.key, certificate: tls.certificate, releases });

    run("npx", ["cap", "sync", "android"], { timeout: 5 * 60_000 });
    run("./gradlew", ["assembleDebug", "-PjariNoFirebase", "--no-daemon"], {
      cwd: join(ROOT, "android"),
      env: {
        JARI_OTA_MANIFEST_URL: `https://localhost:${HTTPS_PORT}/manifest.json`,
        JARI_OTA_PUBLIC_KEY: signing.publicKey,
      },
      timeout: 15 * 60_000,
    });
    const apk = join(ROOT, "android/app/build/outputs/apk/debug/app-debug.apk");
    const installed = adb(serial, ["install", "-r", apk], { capture: true, allowFailure: true });
    if (installed.status !== 0) {
      if (!`${installed.stdout}${installed.stderr}`.includes("INSTALL_FAILED_UPDATE_INCOMPATIBLE")) {
        throw new Error(`APK 설치 실패\n${installed.stdout}${installed.stderr}`);
      }
      adb(serial, ["uninstall", APP]);
      adb(serial, ["install", apk]);
    }
    adb(serial, ["shell", "pm", "clear", APP]);
    adb(serial, ["reverse", `tcp:${HTTPS_PORT}`, `tcp:${HTTPS_PORT}`]);
    adb(serial, ["shell", "am", "start", "-n", `${APP}/${ACTIVITY}`]);
    connection = await connectWebView(serial);

    const initial = await plugin(connection.page, "AppUpdates", "getStatus");
    assertEqual(initial.supported, true, "OTA 플러그인 활성화");
    assertEqual(initial.currentVersion, NATIVE_VERSION, "초기 네이티브 번들 버전");

    server.use("malicious");
    await expectRejected("서명이 변조된 manifest 거부", () => plugin(connection.page, "AppUpdates", "check"));

    server.use("success");
    const available = await plugin(connection.page, "AppUpdates", "check");
    assertEqual(available.status, "available", "정상 업데이트 발견");
    assertEqual(available.version, successVersion, "정상 업데이트 버전");
    await plugin(connection.page, "SecureSession", "write", { token: SESSION_SENTINEL });
    await applyAndWaitForVersion(connection, successVersion);
    await waitFor("정상 번들의 첫 화면", async () => connection.page.evaluate(() => Boolean(
      document.querySelector("#app")?.children.length,
    )));
    await delay(2_000);
    assertEqual((await plugin(connection.page, "SecureSession", "read")).token, SESSION_SENTINEL, "업데이트 후 세션 유지");

    connection = await restartApp(serial, connection);
    assertEqual((await plugin(connection.page, "AppUpdates", "getStatus")).currentVersion, successVersion, "재시작 후 업데이트 유지");
    assertEqual((await plugin(connection.page, "SecureSession", "read")).token, SESSION_SENTINEL, "재시작 후 Keystore 세션 유지");
    await plugin(connection.page, "AppUpdates", "ready", { bootId: (await plugin(connection.page, "AppUpdates", "getStatus")).bootId });
    console.log("✓ 확정 상태의 ready 호출은 안전한 no-op");

    server.use("broken");
    const broken = await plugin(connection.page, "AppUpdates", "check");
    assertEqual(broken.status, "available", "고장 후보 발견");
    const previousBootId = (await plugin(connection.page, "AppUpdates", "getStatus")).bootId;
    await applyAndWaitForVersion(connection, brokenVersion);
    await expectRejected("이전 화면의 ready로 새 후보를 확정하지 않음", () => plugin(connection.page, "AppUpdates", "ready", { bootId: previousBootId }));
    await waitFor("고장 후보 watchdog rollback", async () => {
      try {
        return (await plugin(connection.page, "AppUpdates", "getStatus")).currentVersion === successVersion;
      } catch {
        return false;
      }
    }, 45_000);
    console.log("✓ ready 없는 후보를 30초 watchdog으로 rollback");
    await expectRejected("rollback된 sequence 재생 거부", () => plugin(connection.page, "AppUpdates", "check"));

    server.use("cold");
    assertEqual((await plugin(connection.page, "AppUpdates", "check")).status, "available", "다음 복구 검사 후보 발견");
    await applyAndWaitForVersion(connection, coldVersion);
    connection = await restartApp(serial, connection);
    assertEqual((await plugin(connection.page, "AppUpdates", "getStatus")).currentVersion, successVersion, "미확정 업데이트 도중 종료하면 다음 실행에서 복구");
    assertEqual((await plugin(connection.page, "SecureSession", "read")).token, SESSION_SENTINEL, "복구 후 세션 유지");

    console.log("\n✓ 실제 에뮬레이터 서명 HTTPS OTA smoke 통과");
  } finally {
    await connection?.browser.close().catch(() => undefined);
    if (server) await server.close().catch(() => undefined);
    adb(serial, ["reverse", "--remove", `tcp:${HTTPS_PORT}`], { capture: true, allowFailure: true });
    adb(serial, ["forward", "--remove", `tcp:${CDP_PORT}`], { capture: true, allowFailure: true });
    adb(serial, ["shell", "am", "force-stop", APP], { capture: true, allowFailure: true });
    adb(serial, ["shell", "pm", "clear", APP], { capture: true, allowFailure: true });
    for (const restorePath of restore) restorePath();
    const rawDirectory = dirname(TEST_CA);
    if (existsSync(rawDirectory) && readdirSync(rawDirectory).length === 0) rmdirSync(rawDirectory);
    rmSync(temporary, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.stack || error.message : String(error));
  process.exitCode = 1;
});
