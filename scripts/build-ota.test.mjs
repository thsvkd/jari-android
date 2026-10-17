import { execFileSync, spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, verify } from "node:crypto";
import { linkSync, mkdtempSync, mkdirSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, test } from "vitest";

const SCRIPT = resolve(import.meta.dirname, "build-ota.mjs");
const temporaryDirectories = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    execFileSync(process.execPath, ["-e", "require('fs').rmSync(process.argv[1], { recursive: true, force: true })", directory]);
  }
});

function fixture({ demo = false, nativeVersion = "4.21.0", modulusLength = 2048 } = {}) {
  const root = mkdtempSync(join(tmpdir(), "jari-ota-test-"));
  temporaryDirectories.push(root);
  const dist = join(root, "dist-ota");
  const out = join(root, "release");
  mkdirSync(join(dist, "assets"), { recursive: true });
  writeFileSync(join(dist, "index.html"), "<!doctype html><script src=\"/assets/app.js\"></script>");
  writeFileSync(join(dist, "assets", "app.js"), "globalThis.JARI_OTA = true;\n");
  writeFileSync(join(dist, "ota-build.json"), JSON.stringify({
    apiBaseUrl: "https://jari.example.com/api/mobile",
    demo,
    nativeVersion,
  }));
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength });
  const key = join(root, "private.pem");
  writeFileSync(key, privateKey.export({ type: "pkcs8", format: "pem" }));
  return { root, dist, out, key, publicKey };
}

function runBuild(fixture, extraArgs = []) {
  return spawnSync(process.execPath, [
    SCRIPT,
    "--version", "2026.10.18.1",
    "--sequence", "42",
    "--native-version", "4.21.0",
    "--url", "https://updates.example.com/jari/bundle.zip",
    "--key", fixture.key,
    "--dist", fixture.dist,
    "--out", fixture.out,
    ...extraArgs,
  ], { encoding: "utf8" });
}

function readStoredZip(zip) {
  const entries = new Map();
  const localOffsets = new Map();
  let offset = 0;
  while (zip.readUInt32LE(offset) === 0x04034b50) {
    const compression = zip.readUInt16LE(offset + 8);
    const compressedSize = zip.readUInt32LE(offset + 18);
    const nameLength = zip.readUInt16LE(offset + 26);
    const extraLength = zip.readUInt16LE(offset + 28);
    expect(compression).toBe(0);
    const nameStart = offset + 30;
    const dataStart = nameStart + nameLength + extraLength;
    const name = zip.subarray(nameStart, nameStart + nameLength).toString("utf8");
    localOffsets.set(name, offset);
    entries.set(name, zip.subarray(dataStart, dataStart + compressedSize));
    offset = dataStart + compressedSize;
  }
  const endOffset = zip.length - 22;
  expect(zip.readUInt32LE(endOffset)).toBe(0x06054b50);
  const entryCount = zip.readUInt16LE(endOffset + 10);
  expect(entryCount).toBe(entries.size);
  let centralOffset = zip.readUInt32LE(endOffset + 16);
  for (let index = 0; index < entryCount; index += 1) {
    expect(zip.readUInt32LE(centralOffset)).toBe(0x02014b50);
    const nameLength = zip.readUInt16LE(centralOffset + 28);
    const extraLength = zip.readUInt16LE(centralOffset + 30);
    const commentLength = zip.readUInt16LE(centralOffset + 32);
    const name = zip.subarray(centralOffset + 46, centralOffset + 46 + nameLength).toString("utf8");
    expect(zip.readUInt32LE(centralOffset + 42)).toBe(localOffsets.get(name));
    centralOffset += 46 + nameLength + extraLength + commentLength;
  }
  expect(centralOffset).toBe(endOffset);
  return entries;
}

describe("build-ota", () => {
  test("서명된 manifest와 루트 index.html이 있는 ZIP을 만든다", () => {
    const input = fixture();

    const result = runBuild(input);

    expect(result.status, result.stderr).toBe(0);
    expect(readdirSync(input.out).sort()).toEqual(["bundle.zip", "manifest.json", "public-key.txt"]);
    const zip = readFileSync(join(input.out, "bundle.zip"));
    const manifest = JSON.parse(readFileSync(join(input.out, "manifest.json"), "utf8"));
    const payloadBytes = Buffer.from(manifest.payload, "base64");
    const payload = JSON.parse(payloadBytes.toString("utf8"));
    expect(payload).toEqual({
      schema: 1,
      version: "2026.10.18.1",
      sequence: 42,
      nativeVersion: "4.21.0",
      url: "https://updates.example.com/jari/bundle.zip",
      sha256: createHash("sha256").update(zip).digest("hex"),
    });
    expect(verify("sha256", payloadBytes, input.publicKey, Buffer.from(manifest.signature, "base64"))).toBe(true);
    const tampered = Buffer.from(payloadBytes);
    tampered[tampered.length - 2] ^= 1;
    expect(verify("sha256", tampered, input.publicKey, Buffer.from(manifest.signature, "base64"))).toBe(false);
    expect(readFileSync(join(input.out, "public-key.txt"), "utf8").trim()).toBe(
      input.publicKey.export({ type: "spki", format: "der" }).toString("base64"),
    );

    const entries = readStoredZip(zip);
    expect([...entries.keys()].sort()).toEqual(["assets/app.js", "index.html", "ota-build.json"]);
    expect(entries.get("index.html").toString("utf8")).toContain("/assets/app.js");
    expect(entries.get("assets/app.js").toString("utf8")).toContain("JARI_OTA");
  });

  test.each([
    ["0", "sequence"],
    ["1.5", "sequence"],
    ["9007199254740992", "sequence"],
    ["42", "url", ["--url", "http://updates.example.com/bundle.zip"]],
    ["42", "url", ["--url", "https://user:secret@updates.example.com/bundle.zip"]],
    ["42", "url", ["--url", "https://updates.example.com/bundle.zip#fragment"]],
    ["42", "version", ["--version", "x".repeat(129)]],
  ])("잘못된 입력 sequence=%s를 거부하고 산출물을 남기지 않는다", (sequence, expected, override = []) => {
    const input = fixture();
    const args = override.length > 0 ? override : ["--sequence", sequence];

    const result = runBuild(input, args);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toContain(expected);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test("128자 이하 버전 라벨은 허용한다", () => {
    const input = fixture();

    const result = runBuild(input, ["--version", "x".repeat(128)]);

    expect(result.status, result.stderr).toBe(0);
  });

  test("RSA 개인 키가 아니면 산출물을 남기지 않는다", () => {
    const input = fixture();
    writeFileSync(input.key, "not a private key");

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/private|개인 키|key/i);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test("2048비트보다 작은 RSA 키는 거부한다", () => {
    const input = fixture({ modulusLength: 1024 });

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/2048/);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test("개인 키가 dist 안에 있으면 번들 혼입을 막기 위해 거부한다", () => {
    const input = fixture();
    const bundledKey = join(input.dist, "private.pem");
    writeFileSync(bundledKey, readFileSync(input.key));
    input.key = bundledKey;

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/dist|개인 키/);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test("dist 개인 키를 가리키는 외부 심볼릭 링크 키 경로를 거부한다", () => {
    const input = fixture();
    const bundledKey = join(input.dist, "secret.pem");
    writeFileSync(bundledKey, readFileSync(input.key));
    const externalLink = join(input.root, "external-key.pem");
    symlinkSync(bundledKey, externalLink);
    input.key = externalLink;

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/dist|개인 키/);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test.each([
    ["같은 inode의 하드링크", (input) => linkSync(input.key, join(input.dist, "innocent.dat"))],
    ["이름을 바꾼 PEM 사본", (input) => writeFileSync(join(input.dist, "innocent.txt"), readFileSync(input.key))],
  ])("%s로 개인 키를 번들에 섞지 못한다", (_name, arrange) => {
    const input = fixture();
    arrange(input);

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/개인 키|private key/i);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test("demo 빌드나 nativeVersion이 다른 빌드는 거부한다", () => {
    for (const input of [fixture({ demo: true }), fixture({ nativeVersion: "4.20.0" })]) {
      const result = runBuild(input);
      expect(result.status).not.toBe(0);
      expect(result.stderr).toMatch(/demo|nativeVersion/);
      expect(() => readdirSync(input.out)).toThrow();
    }
  });

  test("심볼릭 링크가 있는 빌드는 거부한다", () => {
    const input = fixture();
    symlinkSync(join(input.dist, "index.html"), join(input.dist, "linked.html"));

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(/symbolic|심볼릭/i);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test.each([
    ["루트 index.html이 없는 빌드", (input) => rmSync(join(input.dist, "index.html")), /index\.html/],
    ["네이티브 실행 파일이 있는 빌드", (input) => writeFileSync(join(input.dist, "payload.so"), "native"), /네이티브 실행 파일/],
    ["localhost API를 쓰는 일반 빌드", (input) => writeFileSync(join(input.dist, "ota-build.json"), JSON.stringify({ apiBaseUrl: "https://localhost:8000/api/mobile", demo: false, nativeVersion: "4.21.0" })), /localhost|루프백/],
    ["IPv4-mapped IPv6 루프백 API를 쓰는 빌드", (input) => writeFileSync(join(input.dist, "ota-build.json"), JSON.stringify({ apiBaseUrl: "https://[::ffff:127.0.0.1]/api/mobile", demo: false, nativeVersion: "4.21.0" })), /루프백/],
    ["안전하지 않은 ZIP 경로명을 가진 빌드", (input) => writeFileSync(join(input.dist, "unsafe\\name.js"), "unsafe"), /경로/],
  ])("%s는 거부한다", (_name, arrange, expected) => {
    const input = fixture();
    arrange(input);

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(result.stderr).toMatch(expected);
    expect(() => readdirSync(input.out)).toThrow();
  });

  test("기존 산출물 디렉터리는 덮어쓰지 않는다", () => {
    const input = fixture();
    mkdirSync(input.out, { recursive: true });
    writeFileSync(join(input.out, "keep.txt"), "do not overwrite");

    const result = runBuild(input);

    expect(result.status).not.toBe(0);
    expect(readFileSync(join(input.out, "keep.txt"), "utf8")).toBe("do not overwrite");
    expect(readdirSync(input.out)).toEqual(["keep.txt"]);
  });
});
