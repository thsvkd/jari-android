#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import { createHash, createPrivateKey, sign } from "node:crypto";
import { existsSync, linkSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

const MAX_APK_BYTES = 128 * 1024 * 1024;
const DIRECT_CERTIFICATE = "cc949e18b5a541a7f533ef954d1b2f675aa0847d413e784234c016e233e34f5e";

function argumentsFor(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const name = argv[i];
    if (!["--apk", "--key", "--url", "--out", "--expected-cert-sha256"].includes(name) || !argv[i + 1] || argv[i + 1].startsWith("--")) {
      throw new Error("사용법: build-apk-update.mjs --apk <signed.apk> --key <private.pem> --url <https APK URL> --out <apk-update.json> [--expected-cert-sha256 <기존 APK 인증서 지문>]");
    }
    options[name.slice(2)] = argv[i + 1];
  }
  for (const name of ["apk", "key", "url", "out"]) if (!options[name]) throw new Error(`--${name}가 필요해요.`);
  return options;
}

function androidTools() {
  const sdk = process.env.ANDROID_HOME || process.env.ANDROID_SDK_ROOT;
  if (!sdk) throw new Error("ANDROID_HOME 또는 ANDROID_SDK_ROOT가 필요해요.");
  const root = join(sdk, "build-tools");
  const versions = readdirSync(root).sort((a, b) => b.localeCompare(a, undefined, { numeric: true }));
  for (const version of versions) {
    const aapt = join(root, version, process.platform === "win32" ? "aapt.exe" : "aapt");
    const apksigner = join(root, version, process.platform === "win32" ? "apksigner.bat" : "apksigner");
    if (existsSync(aapt) && existsSync(apksigner)) return { aapt, apksigner };
  }
  throw new Error("Android build-tools의 aapt와 apksigner를 찾지 못했어요.");
}

function apkMetadata(path, expectedCertificate) {
  const tools = androidTools();
  const output = execFileSync(tools.aapt, ["dump", "badging", path], { encoding: "utf8", timeout: 30_000 });
  const packageLine = output.split(/\r?\n/).find((line) => line.startsWith("package:")) || "";
  const packageName = packageLine.match(/\bname='([^']*)'/)?.[1];
  const code = packageLine.match(/\bversionCode='([^']*)'/)?.[1] || "";
  const version = packageLine.match(/\bversionName='([^']*)'/)?.[1] || "";
  if (packageName !== "dev.thsvkd.jari") throw new Error("APK 패키지가 dev.thsvkd.jari가 아니에요.");
  const versionCode = Number(code);
  if (!/^\d+$/.test(code) || !Number.isSafeInteger(versionCode) || versionCode <= 0 || versionCode > 2_147_483_647) {
    throw new Error("APK versionCode가 유효하지 않아요.");
  }
  if (!version || version.length > 128 || version !== version.trim() || /[\u0000-\u001f\u007f]/u.test(version)) {
    throw new Error("APK versionName이 유효하지 않아요.");
  }
  let certificateOutput;
  try {
    certificateOutput = execFileSync(tools.apksigner, ["verify", "--print-certs", path], { encoding: "utf8", timeout: 30_000, stdio: ["ignore", "pipe", "pipe"] });
  } catch {
    throw new Error("apksigner로 APK 서명을 검증하지 못했어요.");
  }
  const certificates = [...certificateOutput.matchAll(/certificate SHA-256 digest:\s*([0-9a-f]{64})/gi)];
  if (certificates.length !== 1 || certificates[0][1].toLowerCase() !== expectedCertificate) throw new Error("APK 인증서가 기존 직접 배포본의 인증서와 달라요.");
  return { version, versionCode, packageName };
}

function build(options) {
  const url = new URL(options.url);
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.hash) throw new Error("APK URL은 사용자 정보와 fragment가 없는 HTTPS 주소여야 해요.");
  const apk = resolve(options.apk), out = resolve(options.out);
  if (existsSync(out)) throw new Error(`기존 출력은 덮어쓰지 않아요: ${out}`);
  const size = statSync(apk).size;
  if (size <= 0 || size > MAX_APK_BYTES) throw new Error("APK 크기는 0보다 크고 128MiB 이하여야 해요.");
  let key;
  try {
    key = createPrivateKey(readFileSync(options.key));
  } catch {
    throw new Error("RSA 개인 키(key)를 읽지 못했어요.");
  }
  if (key.asymmetricKeyType !== "rsa" || (key.asymmetricKeyDetails?.modulusLength ?? 0) < 2048) throw new Error("RSA 개인 키는 2048비트 이상이어야 해요.");
  const expectedCertificate = (options["expected-cert-sha256"] || DIRECT_CERTIFICATE).replace(/:/g, "").toLowerCase();
  if (!/^[0-9a-f]{64}$/.test(expectedCertificate)) throw new Error("기존 APK 인증서 SHA-256 지문이 유효하지 않아요.");
  const metadata = apkMetadata(apk, expectedCertificate);
  const payload = Buffer.from(JSON.stringify({ schema: 2, ...metadata, url: options.url,
    sha256: createHash("sha256").update(readFileSync(apk)).digest("hex") }));
  const envelope = { payload: payload.toString("base64"), signature: sign("RSA-SHA256", payload, key).toString("base64") };
  mkdirSync(dirname(out), { recursive: true });
  const temporary = `${out}.tmp-${process.pid}-${Date.now()}`;
  try {
    writeFileSync(temporary, `${JSON.stringify(envelope, null, 2)}\n`, { flag: "wx" });
    // Linking publishes a complete file and refuses a concurrently created target.
    linkSync(temporary, out);
  } finally {
    rmSync(temporary, { force: true });
  }
  console.log(`APK 업데이트: ${metadata.version} (${metadata.versionCode}) → ${out}`);
}

try {
  build(argumentsFor(process.argv.slice(2)));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
