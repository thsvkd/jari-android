import { spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, verify } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, expect, it } from "vitest";

const temporary = [];
afterEach(() => temporary.splice(0).forEach((path) => rmSync(path, { recursive: true, force: true })));

function fixture(metadata = {}) {
  const root = mkdtempSync(join(tmpdir(), "jari-apk-manifest-")); temporary.push(root);
  const tools = join(root, "sdk/build-tools/99.0.0"); mkdirSync(tools, { recursive: true });
  // Tool-boundary doubles: the device smoke uses actual signed Android APKs/tools.
  for (const [name, code] of Object.entries({
    aapt: `const d=JSON.parse(require('fs').readFileSync(process.argv.at(-1),'utf8')); console.log("package: name='"+d.packageName+"' versionCode='"+d.versionCode+"' versionName='"+d.version+"'");`,
    apksigner: `const d=JSON.parse(require('fs').readFileSync(process.argv.at(-1),'utf8')); if(d.badSignature)process.exit(1); console.log((d.signerPrefix || 'Signer #1')+' certificate SHA-256 digest: '+(d.certificate || 'ab'.repeat(32))); if(d.additionalCertificate)console.log('Signer #2 certificate SHA-256 digest: '+d.additionalCertificate);`,
  })) {
    const path = join(tools, name); writeFileSync(path, `#!/usr/bin/env node\n${code}\n`); chmodSync(path, 0o700);
  }
  const apk = join(root, "jari.apk");
  writeFileSync(apk, JSON.stringify({ packageName: "dev.thsvkd.jari", versionCode: "42300", version: "4.23.0", ...metadata }));
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const key = join(root, "private.pem"); writeFileSync(key, privateKey.export({ type: "pkcs8", format: "pem" }), { mode: 0o600 });
  return { root, apk, key, publicKey, out: join(root, "apk-update.json"), sdk: join(root, "sdk") };
}

function build(input, args = []) {
  return spawnSync(process.execPath, [resolve("scripts/build-apk-update.mjs"), "--apk", input.apk, "--key", input.key,
    "--url", "https://github.com/thsvkd/jari-android/releases/download/v4.23.0/jari-4.23.0-debug.apk", "--out", input.out,
    "--expected-cert-sha256", "ab".repeat(32), ...args],
  { encoding: "utf8", env: { ...process.env, ANDROID_HOME: input.sdk, ANDROID_SDK_ROOT: input.sdk } });
}

it("signs a package-update manifest using verified APK metadata and the exact file hash", () => {
  const input = fixture(); const result = build(input);
  expect(result.status, result.stderr).toBe(0);
  const envelope = JSON.parse(readFileSync(input.out, "utf8"));
  const payload = Buffer.from(envelope.payload, "base64");
  expect(JSON.parse(payload.toString("utf8"))).toEqual({ schema: 2, version: "4.23.0", versionCode: 42300,
    packageName: "dev.thsvkd.jari", url: "https://github.com/thsvkd/jari-android/releases/download/v4.23.0/jari-4.23.0-debug.apk",
    sha256: createHash("sha256").update(readFileSync(input.apk)).digest("hex") });
  expect(verify("RSA-SHA256", payload, input.publicKey, Buffer.from(envelope.signature, "base64"))).toBe(true);
  expect(readFileSync(input.out, "utf8")).not.toContain("PRIVATE KEY");
});

it("reads the versioned signer prefix emitted by Android build-tools 37", () => {
  const input = fixture({ signerPrefix: "V3.0 Signer:" });
  const result = build(input);
  expect(result.status, result.stderr).toBe(0);
  expect(existsSync(input.out)).toBe(true);
});

it.each([
  [{ packageName: "other.package" }, /package|패키지/],
  [{ versionCode: "0" }, /versionCode/],
  [{ versionCode: "2147483648" }, /versionCode/],
  [{ versionCode: "1.5" }, /versionCode/],
  [{ version: "" }, /version/],
  [{ version: "x".repeat(129) }, /version/],
  [{ badSignature: true }, /signature|서명|apksigner/],
  [{ certificate: "cd".repeat(32) }, /certificate|인증서/],
  [{ signerPrefix: "V3.0 Signer:", certificate: "cd".repeat(32) }, /certificate|인증서/],
  [{ signerPrefix: "V3.0 Signer:", additionalCertificate: "ab".repeat(32) }, /certificate|인증서/],
])("rejects an invalid APK before publishing metadata: %j", (metadata, message) => {
  const input = fixture(metadata); const result = build(input);
  expect(result.status).not.toBe(0); expect(result.stderr).toMatch(message);
  expect(existsSync(input.out)).toBe(false);
});

it.each(["http://example.com/jari.apk", "https://user:password@example.com/jari.apk", "https://example.com/jari.apk#fragment"])("rejects unsafe artifact URL %s", (url) => {
  const input = fixture(); const result = build(input, ["--url", url]);
  expect(result.status).not.toBe(0); expect(result.stderr).toMatch(/https|URL/);
  expect(existsSync(input.out)).toBe(false);
});

it("does not overwrite an existing feed file", () => {
  const input = fixture(); writeFileSync(input.out, "existing");
  const result = build(input);
  expect(result.status).not.toBe(0); expect(readFileSync(input.out, "utf8")).toBe("existing");
});

it("rejects a bad signing key without leaving a manifest", () => {
  const input = fixture(); writeFileSync(input.key, "invalid key");
  const result = build(input);
  expect(result.status).not.toBe(0); expect(result.stderr).toMatch(/key|키/);
  expect(existsSync(input.out)).toBe(false);
});
