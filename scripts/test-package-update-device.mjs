#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, sign } from "node:crypto";
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
const ANDROID = join(ROOT, "android");
const APP = "dev.thsvkd.jari";
const ACTIVITY = "com.jari.app.MainActivity";
const HTTPS_PORT = 23_443;
const CDP_PORT = 9_378;
const API_ORIGIN = "https://package-update-test.invalid";
const SESSION_SENTINEL = "package-update-device-smoke-session-not-a-real-credential";
const LEGACY_OTA_PATH = `/data/user/0/${APP}/files/app-updates/bundles/legacy`;
const LEGACY_MARKER = "legacy-ota-web-bundle-must-not-boot";
const NETWORK_CONFIG = join(ROOT, "android/app/src/debug/res/xml/debug_network_security_config.xml");
const TEST_CA = join(ROOT, "android/app/src/debug/res/raw/jari_package_update_test_ca.pem");
const DIST = join(ROOT, "dist");
const ANDROID_ASSETS = join(ROOT, "android/app/src/main/assets");
const GRADLE_FILE = join(ROOT, "android/app/build.gradle");
const GRADLE = readFileSync(GRADLE_FILE, "utf8");
const BASE_VERSION = GRADLE.match(/versionName\s+"([^"]+)"/)?.[1];
const BASE_VERSION_CODE = Number(GRADLE.match(/versionCode\s+(\d+)/)?.[1]);
const CANDIDATE_VERSION = `${BASE_VERSION}.test-update`;
const CANDIDATE_VERSION_CODE = BASE_VERSION_CODE + 1;

if (process.platform === "darwin") {
  if (!process.env.JAVA_HOME && existsSync("/opt/homebrew/opt/openjdk@21")) process.env.JAVA_HOME = "/opt/homebrew/opt/openjdk@21";
  if (!process.env.ANDROID_HOME && existsSync("/opt/homebrew/share/android-commandlinetools")) process.env.ANDROID_HOME = "/opt/homebrew/share/android-commandlinetools";
  if (process.env.JAVA_HOME) process.env.PATH = `${process.env.JAVA_HOME}/bin:${process.env.PATH}`;
}

if (!BASE_VERSION || !Number.isSafeInteger(BASE_VERSION_CODE) || BASE_VERSION_CODE <= 0) {
  throw new Error("android versionName/versionCode를 읽지 못했어요.");
}

function run(command, args, { cwd = ROOT, env = {}, capture = false, allowFailure = false, quiet = false, timeout = 120_000 } = {}) {
  const printable = [command, ...args].map((part) => part.includes(" ") ? JSON.stringify(part) : part).join(" ");
  if (!quiet) console.log(`\n$ ${printable}`);
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
    throw new Error("이 검증은 설치 앱을 지워요. ANDROID_SERIAL=emulator-… 를 명시해야 하며 실기기에서는 실행할 수 없어요.");
  }
  const devices = run("adb", ["devices"], { capture: true }).stdout
    .split(/\r?\n/)
    .slice(1)
    .filter((line) => /\tdevice$/.test(line))
    .map((line) => line.split("\t")[0]);
  if (!devices.includes(serial)) throw new Error(`연결된 에뮬레이터가 아니에요: ${serial}`);
  return serial;
}

function adb(serial, args, options = {}) {
  return run("adb", ["-s", serial, ...args], options);
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

function createSigningKey() {
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  return {
    privateKey,
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
            <certificates src="@raw/jari_package_update_test_ca" />
        </trust-anchors>
    </base-config>
</network-security-config>
`);
}

function writeVersionOverride(temporary) {
  const initScript = join(temporary, "package-update-version.gradle");
  writeFileSync(initScript, `allprojects { project ->
    afterEvaluate {
        if (project.path == ':app') {
            project.android.defaultConfig.versionCode = Integer.parseInt(gradle.startParameter.projectProperties['jariSmokeVersionCode'])
            project.android.defaultConfig.versionName = gradle.startParameter.projectProperties['jariSmokeVersionName']
        }
    }
}
`);
  return initScript;
}

function buildApk({ initScript, output, version, versionCode, updatePublicKey }) {
  run("./gradlew", [
    "clean", "assembleDebug", "-PjariNoFirebase", "--no-daemon",
    "-I", initScript,
    `-PjariSmokeVersionCode=${versionCode}`,
    `-PjariSmokeVersionName=${version}`,
  ], {
    cwd: ANDROID,
    env: {
      JARI_APK_UPDATE_MANIFEST_URL: `https://localhost:${HTTPS_PORT}/manifest.json`,
      JARI_APK_UPDATE_PUBLIC_KEY: updatePublicKey,
    },
    timeout: 15 * 60_000,
  });
  copyFileSync(join(ANDROID, "app/build/outputs/apk/debug/app-debug.apk"), output);
}

function sdkTool(name) {
  const sdk = process.env.ANDROID_HOME || process.env.ANDROID_SDK_ROOT;
  if (!sdk) throw new Error("ANDROID_HOME 또는 ANDROID_SDK_ROOT가 필요해요.");
  const buildTools = join(sdk, "build-tools");
  const versions = existsSync(buildTools) ? readdirSync(buildTools).sort((a, b) => b.localeCompare(a, undefined, { numeric: true })) : [];
  const path = versions.map((version) => join(buildTools, version, name)).find(existsSync);
  if (!path) throw new Error(`Android SDK ${name} 도구를 찾지 못했어요.`);
  return path;
}

function inspectApk(apk) {
  const badging = run(sdkTool("aapt"), ["dump", "badging", apk], { capture: true }).stdout;
  const packageLine = badging.split(/\r?\n/).find((line) => line.startsWith("package: ")) || "";
  const packageName = packageLine.match(/name='([^']+)'/)?.[1];
  const versionCode = Number(packageLine.match(/versionCode='(\d+)'/)?.[1]);
  const version = packageLine.match(/versionName='([^']+)'/)?.[1];
  const signer = run(sdkTool("apksigner"), ["verify", "--print-certs", apk], { capture: true }).stdout
    .match(/certificate SHA-256 digest:\s*([0-9a-f]{64})/i)?.[1]?.toLowerCase();
  if (!packageName || !Number.isSafeInteger(versionCode) || !version || !signer) {
    throw new Error(`APK 메타데이터를 읽지 못했어요: ${apk}`);
  }
  return { packageName, versionCode, version, signer };
}

function assertEqual(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label}: 기대 ${JSON.stringify(expected)}, 실제 ${JSON.stringify(actual)}`);
  console.log(`✓ ${label}`);
}

function assertApk(apk, expected) {
  const actual = inspectApk(apk);
  assertEqual(actual.packageName, APP, `${expected.label} packageName`);
  assertEqual(actual.versionCode, expected.versionCode, `${expected.label} versionCode`);
  assertEqual(actual.version, expected.version, `${expected.label} versionName`);
  return actual;
}

function digest(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function signedManifest({ apk, privateKey }) {
  const payload = JSON.stringify({
    schema: 2,
    version: CANDIDATE_VERSION,
    versionCode: CANDIDATE_VERSION_CODE,
    packageName: APP,
    url: `https://localhost:${HTTPS_PORT}/jari-${CANDIDATE_VERSION_CODE}.apk`,
    sha256: digest(apk),
  });
  const bytes = Buffer.from(payload, "utf8");
  return Buffer.from(JSON.stringify({
    payload: bytes.toString("base64"),
    signature: sign("RSA-SHA256", bytes, privateKey).toString("base64"),
  }));
}

async function startUpdateServer({ key, certificate, apk, manifest }) {
  let currentManifest = manifest;
  const apkBody = readFileSync(apk);
  const server = createServer({ key: readFileSync(key), cert: readFileSync(certificate) }, (request, response) => {
    const path = new URL(request.url, `https://localhost:${HTTPS_PORT}`).pathname;
    const body = path === "/manifest.json"
      ? currentManifest
      : path === `/jari-${CANDIDATE_VERSION_CODE}.apk` ? apkBody : undefined;
    if (!body) {
      response.writeHead(404, { "Content-Type": "text/plain", "Cache-Control": "no-store" });
      response.end("not found");
      return;
    }
    response.writeHead(200, {
      "Content-Type": path.endsWith(".apk") ? "application/vnd.android.package-archive" : "application/json",
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
    tamper() {
      const envelope = JSON.parse(manifest.toString("utf8"));
      envelope.signature = `${envelope.signature.slice(0, -2)}AA`;
      currentManifest = Buffer.from(JSON.stringify(envelope));
    },
    restore() {
      currentManifest = manifest;
    },
    close: () => new Promise((complete, reject) => server.close((error) => error ? reject(error) : complete())),
  };
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
    const result = adb(serial, ["shell", "pidof", APP], { capture: true, allowFailure: true, quiet: true });
    return result.status === 0 && result.stdout.trim() || undefined;
  });
  const socket = await waitFor("WebView 디버깅 소켓", async () => {
    const output = adb(serial, ["shell", "cat", "/proc/net/unix"], { capture: true, quiet: true }).stdout;
    return output.split(/\r?\n/).map((line) => line.trim().split(/\s+/).pop())
      .find((name) => name === `@webview_devtools_remote_${pid}`);
  });
  adb(serial, ["forward", "--remove", `tcp:${CDP_PORT}`], { allowFailure: true, capture: true });
  adb(serial, ["forward", `tcp:${CDP_PORT}`, `localabstract:${socket.slice(1)}`]);
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${CDP_PORT}`, { noDefaults: true });
  const page = await waitFor("앱 WebView 페이지", async () => browser.contexts()[0]?.pages()[0]);
  await waitFor("Capacitor 업데이트·세션 플러그인", async () => page.evaluate(() => Boolean(
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

function installedVersionCode(serial) {
  const output = adb(serial, ["shell", "dumpsys", "package", APP], { capture: true, allowFailure: true, quiet: true }).stdout;
  return Number(output.match(/versionCode=(\d+)/)?.[1] || 0);
}

function runAs(serial, args, options = {}) {
  return adb(serial, ["shell", "run-as", APP, ...args], options);
}

function runAsExists(serial, path) {
  return runAs(serial, ["test", "-e", path], { capture: true, allowFailure: true, quiet: true }).status === 0;
}

function readRunAsFile(serial, path) {
  const result = runAs(serial, ["cat", path], { capture: true, allowFailure: true, quiet: true });
  return result.status === 0 ? result.stdout : "";
}

function copyTestFileIntoApp(serial, local, remoteName, destination) {
  const remote = `/data/local/tmp/${remoteName}`;
  adb(serial, ["push", local, remote]);
  adb(serial, ["shell", "chmod", "0644", remote]);
  try {
    runAs(serial, ["cp", remote, destination]);
  } finally {
    adb(serial, ["shell", "rm", "-f", remote], { capture: true, allowFailure: true });
  }
}

function seedLegacyOtaState(serial, temporary) {
  const marker = join(temporary, "legacy-index.html");
  const otaPreferences = join(temporary, "jari_app_updates.xml");
  const webViewPreferences = join(temporary, "CapWebViewSettings.xml");
  writeFileSync(marker, `<!doctype html><html><body><main id="legacy-ota-marker">${LEGACY_MARKER}</main></body></html>`);
  writeFileSync(otaPreferences, `<?xml version="1.0" encoding="utf-8" standalone="yes" ?>
<map>
    <string name="binary">42201:4.22.0</string>
    <string name="activeVersion">4.22.0-web.2</string>
    <string name="activePath">${LEGACY_OTA_PATH}</string>
    <long name="activeSequence" value="2" />
    <long name="highestSequence" value="2" />
    <boolean name="pendingReady" value="false" />
</map>
`);
  writeFileSync(webViewPreferences, `<?xml version="1.0" encoding="utf-8" standalone="yes" ?>
<map>
    <string name="serverBasePath">${LEGACY_OTA_PATH}</string>
</map>
`);

  runAs(serial, ["mkdir", "-p", "files/app-updates/bundles/legacy", "shared_prefs"]);
  copyTestFileIntoApp(serial, marker, "jari-legacy-index.html", "files/app-updates/bundles/legacy/index.html");
  copyTestFileIntoApp(serial, otaPreferences, "jari_app_updates.xml", "shared_prefs/jari_app_updates.xml");
  copyTestFileIntoApp(serial, webViewPreferences, "CapWebViewSettings.xml", "shared_prefs/CapWebViewSettings.xml");

  assertEqual(runAsExists(serial, "files/app-updates/bundles/legacy/index.html"), true, "이전 OTA 번들 상태 준비");
  assertEqual(readRunAsFile(serial, "shared_prefs/jari_app_updates.xml").includes("4.22.0-web.2"), true, "이전 OTA 상태 preference 준비");
  assertEqual(readRunAsFile(serial, "shared_prefs/CapWebViewSettings.xml").includes("serverBasePath"), true, "이전 WebView 경로 preference 준비");
}

async function assertLegacyOtaStateCleared(serial, page) {
  await waitFor("이전 OTA 파일과 preference 정리", async () => {
    const otaPreferences = readRunAsFile(serial, "shared_prefs/jari_app_updates.xml");
    const webViewPreferences = readRunAsFile(serial, "shared_prefs/CapWebViewSettings.xml");
    // Capacitor 자체의 binary-version 전환도 serverBasePath를 빈 문자열로 기록할 수 있어요.
    const serverPathCleared = !webViewPreferences.includes('name="serverBasePath"')
      || /<string name="serverBasePath">\s*<\/string>/.test(webViewPreferences);
    return !runAsExists(serial, "files/app-updates")
      && !otaPreferences.includes("activeVersion")
      && !otaPreferences.includes(LEGACY_OTA_PATH)
      && serverPathCleared
      && !webViewPreferences.includes(LEGACY_OTA_PATH);
  });
  console.log("✓ 이전 OTA 파일·상태·WebView 경로 정리");

  const bundled = await waitFor("새 APK의 내장 WebView 화면", async () => {
    const state = await page.evaluate((legacyMarker) => ({
      legacyVisible: document.body.textContent?.includes(legacyMarker) === true || Boolean(document.querySelector("#legacy-ota-marker")),
      appVisible: Boolean(document.querySelector("#app > .auth-shell, #app > .app-shell, #app > .fatal")),
      updatesAvailable: Boolean(window.Capacitor?.Plugins?.AppUpdates),
    }), LEGACY_MARKER);
    return state.appVisible && state.updatesAvailable ? state : undefined;
  });
  assertEqual(bundled.legacyVisible, false, "이전 OTA WebView를 열지 않음");
  assertEqual(bundled.appVisible, true, "새 APK에 포함된 앱 화면으로 부팅");
  assertEqual(bundled.updatesAvailable, true, "새 APK의 네이티브 업데이트 플러그인 연결");
}

function decodeXml(value) {
  return value.replaceAll("&quot;", '"').replaceAll("&amp;", "&").replaceAll("&lt;", "<").replaceAll("&gt;", ">");
}

function uiNodes(serial) {
  const remote = "/data/local/tmp/jari-package-update-window.xml";
  adb(serial, ["shell", "rm", "-f", remote], { capture: true, allowFailure: true, quiet: true });
  adb(serial, ["shell", "uiautomator", "dump", remote], { capture: true, allowFailure: true, quiet: true });
  const xml = adb(serial, ["shell", "cat", remote], { capture: true, allowFailure: true, quiet: true }).stdout;
  return [...xml.matchAll(/<node\s+([^>]+?)\/?\s*>/g)].map(([, attributes]) => {
    const fields = Object.fromEntries([...attributes.matchAll(/([\w-]+)="([^"]*)"/g)].map(([, key, value]) => [key, decodeXml(value)]));
    const bounds = fields.bounds?.match(/\[(\d+),(\d+)]\[(\d+),(\d+)]/);
    return {
      ...fields,
      center: bounds ? [Math.round((Number(bounds[1]) + Number(bounds[3])) / 2), Math.round((Number(bounds[2]) + Number(bounds[4])) / 2)] : undefined,
    };
  });
}

function matchesNode(node, patterns) {
  const values = [node.text || "", node["content-desc"] || "", node["resource-id"] || ""];
  return patterns.some((pattern) => values.some((value) => pattern.test(value)));
}

function clickNode(serial, node, label) {
  adb(serial, ["shell", "input", "tap", String(node.center[0]), String(node.center[1])]);
  console.log(`✓ ${label}`);
}

async function allowUnknownSourcesIfShown(serial) {
  const destination = await waitFor("알 수 없는 앱 설정 또는 설치 화면", async () => {
    const nodes = uiNodes(serial);
    const setting = nodes.find((node) => matchesNode(node, [/Allow from this source/i, /이 출처 허용/, /알 수 없는 앱 설치/]));
    if (setting) return { type: "setting", nodes };
    const installer = nodes.find((node) => matchesNode(node, [/^Install$/i, /^Update$/i, /^설치$/, /^업데이트$/]));
    return installer ? { type: "installer", nodes } : undefined;
  });
  if (destination.type === "installer") return;

  const toggle = destination.nodes.find((node) => node.center && node.checkable === "true")
    || destination.nodes.find((node) => node.center && matchesNode(node, [/Allow from this source/i, /이 출처 허용/]))
    || destination.nodes.find((node) => node.center && /Switch/.test(node.class || ""));
  if (!toggle) throw new Error("알 수 없는 앱 설치 허용 스위치를 찾지 못했어요.");
  if (toggle.checked !== "true") {
    clickNode(serial, toggle, "알 수 없는 앱 설치 허용");
    await waitFor("알 수 없는 앱 설치 권한 반영", async () => uiNodes(serial).some((node) => (
      node.checked === "true" && (
        node.checkable === "true" || matchesNode(node, [/Allow from this source/i, /이 출처 허용/]) || /Switch/.test(node.class || "")
      )
    )));
  }
  adb(serial, ["shell", "input", "keyevent", "4"]);
}

const INSTALL_BUTTONS = [/^Install$/i, /^Update$/i, /^설치$/, /^업데이트$/];

// 설치기는 확인 창 앞에 "Staging app…" 창을 띄워요. 그 창의 하나뿐인 취소 버튼은 확인 창의 업데이트
// 버튼 자리(또는 가운데로 옮기기 전의 위쪽)에 있어서, 덤프와 탭 사이에 확인 창으로 바뀌면 엉뚱한 버튼을
// 눌러요. 설치/업데이트 버튼이 있는 확인 창에서, 두 번 연속 같은 자리에 있는 버튼만 돌려줘요.
async function waitForConfirmDialogButton(serial, label, patterns) {
  let previous;
  return waitFor(label, async () => {
    const nodes = uiNodes(serial).filter((node) => node.center);
    const current = nodes.some((node) => matchesNode(node, INSTALL_BUTTONS))
      ? nodes.find((node) => matchesNode(node, patterns))
      : undefined;
    const stable = current && previous && current.bounds === previous.bounds;
    previous = current;
    return stable ? current : undefined;
  }, 60_000, 750);
}

async function cancelInstaller(serial) {
  const cancel = await waitForConfirmDialogButton(serial, "패키지 설치 확인 창의 취소 버튼", [/^Cancel$/i, /^취소$/]);
  clickNode(serial, cancel, "첫 패키지 설치 취소");
}

async function confirmInstaller(serial) {
  const install = await waitForConfirmDialogButton(serial, "패키지 설치 확인 버튼", INSTALL_BUTTONS);
  clickNode(serial, install, "Android 시스템 패키지 설치 확인");
}

async function restartAndConnect(serial, connection) {
  await connection?.browser.close().catch(() => undefined);
  adb(serial, ["shell", "am", "force-stop", APP], { capture: true, allowFailure: true });
  adb(serial, ["shell", "am", "start", "-n", `${APP}/${ACTIVITY}`]);
  return connectWebView(serial);
}

async function main() {
  const javaVersion = run("java", ["-version"], { capture: true, quiet: true });
  const javaMajor = Number(`${javaVersion.stderr}${javaVersion.stdout}`.match(/version "(\d+)/)?.[1]);
  if (!Number.isInteger(javaMajor) || javaMajor < 21) {
    throw new Error("APK 기기 검증에는 JDK 21 이상이 필요해요. JAVA_HOME을 확인해 주세요.");
  }
  const serial = requireEmulator();
  const temporary = mkdtempSync(join(tmpdir(), "jari-package-update-device-"));
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
  let ownsInstalledApp = false;
  try {
    const signing = createSigningKey();
    const tls = createLocalCertificate(temporary);
    installDebugCa(tls.certificate);

    if (process.env.JARI_PACKAGE_UPDATE_PREPARED_ASSETS !== "1") {
      run("npm", ["run", "build"], {
        env: { VITE_API_BASE_URL: API_ORIGIN, VITE_DEMO_MODE: "false" },
        timeout: 10 * 60_000,
      });
      run("npx", ["cap", "sync", "android"], { timeout: 5 * 60_000 });
    }

    const initScript = writeVersionOverride(temporary);
    const baseApk = join(temporary, `jari-${BASE_VERSION_CODE}.apk`);
    const candidateApk = join(temporary, `jari-${CANDIDATE_VERSION_CODE}.apk`);
    buildApk({ initScript, output: baseApk, version: BASE_VERSION, versionCode: BASE_VERSION_CODE, updatePublicKey: signing.publicKey });
    buildApk({ initScript, output: candidateApk, version: CANDIDATE_VERSION, versionCode: CANDIDATE_VERSION_CODE, updatePublicKey: signing.publicKey });

    const baseMetadata = assertApk(baseApk, { label: "기준 APK", version: BASE_VERSION, versionCode: BASE_VERSION_CODE });
    const candidateMetadata = assertApk(candidateApk, { label: "후보 APK", version: CANDIDATE_VERSION, versionCode: CANDIDATE_VERSION_CODE });
    assertEqual(candidateMetadata.signer, baseMetadata.signer, "두 APK의 서명 인증서 일치");

    const manifest = signedManifest({ apk: candidateApk, privateKey: signing.privateKey });
    server = await startUpdateServer({ key: tls.key, certificate: tls.certificate, apk: candidateApk, manifest });

    ownsInstalledApp = true;
    adb(serial, ["uninstall", APP], { capture: true, allowFailure: true });
    adb(serial, ["install", "-i", "com.google.android.packageinstaller", baseApk]);
    adb(serial, ["shell", "pm", "clear", APP]);
    adb(serial, ["shell", "appops", "set", APP, "REQUEST_INSTALL_PACKAGES", "deny"], { capture: true, allowFailure: true });
    adb(serial, ["reverse", `tcp:${HTTPS_PORT}`, `tcp:${HTTPS_PORT}`]);
    adb(serial, ["shell", "am", "start", "-n", `${APP}/${ACTIVITY}`]);
    connection = await connectWebView(serial);

    const initial = await plugin(connection.page, "AppUpdates", "getStatus");
    assertEqual(initial.supported, true, "직접 설치 APK 업데이트 활성화");
    assertEqual(initial.source, "apk", "업데이트 출처 APK");
    assertEqual(initial.currentVersion, BASE_VERSION, "설치된 기준 versionName");
    assertEqual(initial.versionCode, BASE_VERSION_CODE, "설치된 기준 versionCode");
    assertEqual(installedVersionCode(serial), BASE_VERSION_CODE, "시스템 기준 versionCode");
    await waitFor("기준 APK의 내장 로그인 화면", async () => connection.page.evaluate(() => Boolean(
      document.querySelector("#app > .auth-shell"),
    )));
    console.log("✓ 기준 APK가 내장 로그인 화면으로 부팅");

    server.tamper();
    await expectRejected("서명이 변조된 manifest 거부", () => plugin(connection.page, "AppUpdates", "check"));
    server.restore();
    const available = await plugin(connection.page, "AppUpdates", "check");
    assertEqual(available.status, "available", "새 APK 발견");
    assertEqual(available.version, CANDIDATE_VERSION, "새 APK versionName");
    assertEqual(available.versionCode, CANDIDATE_VERSION_CODE, "새 APK versionCode");

    await plugin(connection.page, "SecureSession", "write", { token: SESSION_SENTINEL });
    const cancelledApply = plugin(connection.page, "AppUpdates", "apply");
    // UI 검사 실패로 finally에서 WebView를 닫아도 대기 중인 호출이 미처리 rejection이 되지 않게 해요.
    cancelledApply.catch(() => undefined);
    await allowUnknownSourcesIfShown(serial);
    await cancelInstaller(serial);
    assertEqual((await cancelledApply).status, "cancelled", "시스템 설치 취소 결과");
    assertEqual(installedVersionCode(serial), BASE_VERSION_CODE, "취소 후 기준 APK 유지");

    const retry = await plugin(connection.page, "AppUpdates", "check");
    assertEqual(retry.status, "available", "취소 후 업데이트 재시도 가능");
    // 4.22.0-web.2가 남긴 앱 전용 파일과 preference를 흉내 내요. 현재 프로세스는 이미
    // WebView를 열었으므로, 다음 차가운 시작에서 새 APK가 이것을 치우는지 검증할 수 있어요.
    seedLegacyOtaState(serial, temporary);
    const startedApply = plugin(connection.page, "AppUpdates", "apply").catch((error) => {
      if (!/Execution context was destroyed|Target page, context or browser has been closed/.test(error.message)) throw error;
      return undefined;
    });
    startedApply.catch(() => undefined);
    await allowUnknownSourcesIfShown(serial);
    await confirmInstaller(serial);
    await waitFor("새 APK 시스템 설치", async () => installedVersionCode(serial) === CANDIDATE_VERSION_CODE, 90_000);
    console.log("✓ Android 패키지 관리자가 새 APK를 설치함");
    await Promise.race([startedApply, delay(5_000)]);

    connection = await restartAndConnect(serial, connection);
    const updated = await plugin(connection.page, "AppUpdates", "getStatus");
    assertEqual(updated.source, "apk", "재시작 후 업데이트 출처 APK");
    assertEqual(updated.currentVersion, CANDIDATE_VERSION, "재시작 후 후보 versionName");
    assertEqual(updated.versionCode, CANDIDATE_VERSION_CODE, "재시작 후 후보 versionCode");
    assertEqual((await plugin(connection.page, "AppUpdates", "check")).status, "current", "설치된 후보를 최신으로 판정");
    assertEqual((await plugin(connection.page, "SecureSession", "read")).token, SESSION_SENTINEL, "패키지 교체 후 Keystore 세션 유지");
    await assertLegacyOtaStateCleared(serial, connection.page);

    console.log("\n✓ 실제 Android 시스템 APK 패키지 업데이트 smoke 통과");
  } finally {
    await connection?.browser.close().catch(() => undefined);
    if (server) await server.close().catch(() => undefined);
    adb(serial, ["reverse", "--remove", `tcp:${HTTPS_PORT}`], { capture: true, allowFailure: true });
    adb(serial, ["forward", "--remove", `tcp:${CDP_PORT}`], { capture: true, allowFailure: true });
    if (ownsInstalledApp) {
      adb(serial, ["shell", "am", "force-stop", APP], { capture: true, allowFailure: true });
      // 다음 기기 e2e가 낮은 원래 versionCode APK를 설치할 수 있게 정리해요. 기준→후보 사이에는 앱이나 데이터를 지우지 않아요.
      adb(serial, ["uninstall", APP], { capture: true, allowFailure: true });
    }
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
