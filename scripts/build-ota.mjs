#!/usr/bin/env node

import {
  createHash,
  createPrivateKey,
  createPublicKey,
  sign,
} from "node:crypto";
import {
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";

const MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024;
const MAX_EXPANDED_BYTES = 50 * 1024 * 1024;
const MAX_ENTRIES = 2_000;
const FORBIDDEN_EXTENSIONS = new Set([
  ".aab", ".apk", ".class", ".dex", ".dll", ".dylib", ".exe", ".jar", ".so",
]);

function parseArguments(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const option = argv[index];
    const value = argv[index + 1];
    if (!option?.startsWith("--") || value === undefined || value.startsWith("--")) {
      throw new Error(`옵션과 값을 함께 지정해 주세요: ${option || "(없음)"}`);
    }
    values.set(option.slice(2), value);
  }

  const required = ["version", "sequence", "native-version", "url", "key"];
  for (const name of required) {
    if (!values.get(name)?.trim()) throw new Error(`--${name} 값이 필요해요.`);
  }
  for (const name of values.keys()) {
    if (![...required, "dist", "out"].includes(name)) throw new Error(`알 수 없는 옵션이에요: --${name}`);
  }

  const sequence = Number(values.get("sequence"));
  if (!Number.isSafeInteger(sequence) || sequence <= 0) {
    throw new Error("--sequence 는 1 이상의 JavaScript safe integer여야 해요.");
  }
  const url = new URL(values.get("url"));
  if (url.protocol !== "https:") throw new Error("--url 은 https URL이어야 해요.");
  if (url.username || url.password || url.hash) {
    throw new Error("--url 에 사용자 정보나 fragment를 넣을 수 없어요.");
  }
  for (const name of ["version", "native-version"]) {
    const value = values.get(name).trim();
    if (value.length < 1 || value.length > 128) throw new Error(`--${name} 은 1~128자여야 해요.`);
  }

  return {
    version: values.get("version").trim(),
    sequence,
    nativeVersion: values.get("native-version").trim(),
    url: url.href,
    key: resolve(values.get("key")),
    dist: resolve(values.get("dist") || "dist-ota"),
    out: resolve(values.get("out") || "dist-release/ota"),
  };
}

function extension(name) {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot).toLowerCase();
}

function hasNativeExecutableHeader(content) {
  if (content.length < 4) return false;
  const first = content.readUInt32BE(0);
  return first === 0x7f454c46 // ELF
    || first === 0xcafebabe // universal Mach-O / Java class (both forbidden)
    || first === 0xfeedface || first === 0xfeedfacf
    || first === 0xcefaedfe || first === 0xcffaedfe
    || (content[0] === 0x4d && content[1] === 0x5a) // PE/Windows executable
    || content.subarray(0, 4).toString("ascii") === "dex\n";
}

function containsPrivateKeyPem(content) {
  return [
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
  ].some((marker) => content.includes(Buffer.from(marker)));
}

function collectEntries(dist, privateKeyIdentity, privateKeyContent) {
  const root = lstatSync(dist);
  if (!root.isDirectory()) throw new Error(`빌드 디렉터리가 아니에요: ${dist}`);
  const entries = [];
  let expandedBytes = 0;

  function visit(directory) {
    for (const item of readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const absolute = join(directory, item.name);
      const info = lstatSync(absolute);
      const name = relative(dist, absolute).split(sep).join("/");
      const segments = name.split("/");
      if (name.startsWith("/") || name.includes("\\") || /[\u0000-\u001f\u007f]/.test(name)
        || segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
        throw new Error(`안전하지 않은 OTA 경로예요: ${JSON.stringify(name)}`);
      }
      if (info.isSymbolicLink()) throw new Error(`심볼릭 링크는 OTA에 넣을 수 없어요: ${name}`);
      if (info.isDirectory()) {
        visit(absolute);
        continue;
      }
      if (!info.isFile()) throw new Error(`일반 파일이 아닌 항목은 OTA에 넣을 수 없어요: ${name}`);
      if (info.dev === privateKeyIdentity.dev && info.ino === privateKeyIdentity.ino) {
        throw new Error(`개인 키와 같은 파일은 OTA에 넣을 수 없어요: ${name}`);
      }
      if ((info.mode & 0o111) !== 0 || FORBIDDEN_EXTENSIONS.has(extension(name))) {
        throw new Error(`네이티브 실행 파일은 OTA에 넣을 수 없어요: ${name}`);
      }
      const content = readFileSync(absolute);
      if (content.equals(privateKeyContent)) throw new Error(`개인 키 사본은 OTA에 넣을 수 없어요: ${name}`);
      if (containsPrivateKeyPem(content)) throw new Error(`private key 내용은 OTA에 넣을 수 없어요: ${name}`);
      if (hasNativeExecutableHeader(content)) throw new Error(`네이티브 실행 파일은 OTA에 넣을 수 없어요: ${name}`);
      expandedBytes += content.length;
      if (expandedBytes > MAX_EXPANDED_BYTES) throw new Error("OTA 압축 해제 크기는 50MiB를 넘을 수 없어요.");
      entries.push({ name, content });
      if (entries.length > MAX_ENTRIES) throw new Error(`OTA 파일 수는 ${MAX_ENTRIES}개를 넘을 수 없어요.`);
    }
  }

  visit(dist);
  if (!entries.some(({ name }) => name === "index.html")) {
    throw new Error("dist-ota 루트에 index.html이 필요해요.");
  }
  return entries;
}

function isLoopbackHostname(rawHostname) {
  const hostname = rawHostname.toLowerCase().replace(/^\[|\]$/g, "");
  if (hostname === "localhost" || hostname.endsWith(".localhost") || hostname === "0.0.0.0"
    || hostname === "::1" || /^127(?:\.\d{1,3}){3}$/.test(hostname)) return true;
  if (!hostname.startsWith("::ffff:")) return false;
  const mapped = hostname.slice("::ffff:".length);
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(mapped)) return mapped.startsWith("127.") || mapped === "0.0.0.0";
  const words = mapped.split(":");
  if (words.length !== 2 || words.some((word) => !/^[0-9a-f]{1,4}$/.test(word))) return false;
  const ipv4 = (Number.parseInt(words[0], 16) * 0x10000) + Number.parseInt(words[1], 16);
  return (ipv4 >>> 24) === 127 || ipv4 === 0;
}

function readProvenance(dist, nativeVersion) {
  let provenance;
  try {
    provenance = JSON.parse(readFileSync(join(dist, "ota-build.json"), "utf8"));
  } catch (error) {
    throw new Error(`dist-ota/ota-build.json을 읽을 수 없어요: ${error.message}`);
  }
  if (provenance.demo !== false) throw new Error("demo 빌드는 OTA로 배포할 수 없어요.");
  if (provenance.nativeVersion !== nativeVersion) {
    throw new Error(`ota-build.json nativeVersion이 --native-version과 달라요: ${provenance.nativeVersion}`);
  }
  let apiUrl;
  try {
    apiUrl = new URL(provenance.apiBaseUrl);
  } catch {
    throw new Error("ota-build.json apiBaseUrl은 유효한 https URL이어야 해요.");
  }
  if (apiUrl.protocol !== "https:") throw new Error("ota-build.json apiBaseUrl은 https URL이어야 해요.");
  if (isLoopbackHostname(apiUrl.hostname)) {
    throw new Error("ota-build.json apiBaseUrl은 localhost나 루프백 주소를 사용할 수 없어요.");
  }
  if (apiUrl.username || apiUrl.password || apiUrl.hash) {
    throw new Error("ota-build.json apiBaseUrl에 사용자 정보나 fragment를 넣을 수 없어요.");
  }
}

const crcTable = Array.from({ length: 256 }, (_, start) => {
  let value = start;
  for (let bit = 0; bit < 8; bit += 1) value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
  return value >>> 0;
});

function crc32(content) {
  let value = 0xffffffff;
  for (const byte of content) value = crcTable[(value ^ byte) & 0xff] ^ (value >>> 8);
  return (value ^ 0xffffffff) >>> 0;
}

function buildZip(entries) {
  const localRecords = [];
  const centralRecords = [];
  let offset = 0;
  for (const { name, content } of entries) {
    const encodedName = Buffer.from(name, "utf8");
    const checksum = crc32(content);
    const local = Buffer.alloc(30 + encodedName.length);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6);
    local.writeUInt16LE(0, 8);
    local.writeUInt16LE(0, 10);
    local.writeUInt16LE(0x0021, 12); // 1980-01-01
    local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(content.length, 18);
    local.writeUInt32LE(content.length, 22);
    local.writeUInt16LE(encodedName.length, 26);
    encodedName.copy(local, 30);
    localRecords.push(local, content);

    const central = Buffer.alloc(46 + encodedName.length);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(0x0314, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt16LE(0, 12);
    central.writeUInt16LE(0x0021, 14);
    central.writeUInt32LE(checksum, 16);
    central.writeUInt32LE(content.length, 20);
    central.writeUInt32LE(content.length, 24);
    central.writeUInt16LE(encodedName.length, 28);
    central.writeUInt32LE((0o100644 << 16) >>> 0, 38);
    central.writeUInt32LE(offset, 42);
    encodedName.copy(central, 46);
    centralRecords.push(central);
    offset += local.length + content.length;
  }

  const centralDirectory = Buffer.concat(centralRecords);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralDirectory.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...localRecords, centralDirectory, end]);
}

function loadPrivateKey(content) {
  let privateKey;
  try {
    privateKey = createPrivateKey(content);
  } catch (error) {
    throw new Error(`RSA private key를 읽을 수 없어요: ${error.message}`);
  }
  if (privateKey.type !== "private" || privateKey.asymmetricKeyType !== "rsa") {
    throw new Error("--key는 RSA private key여야 해요.");
  }
  if ((privateKey.asymmetricKeyDetails?.modulusLength ?? 0) < 2048) {
    throw new Error("--key는 2048비트 이상의 RSA private key여야 해요.");
  }
  return privateKey;
}

function build(options) {
  if (basename(options.out) === "." || options.out === dirname(options.out)) throw new Error("안전한 --out 경로가 필요해요.");
  try {
    lstatSync(options.out);
    throw new Error(`기존 출력은 덮어쓰지 않아요: ${options.out}`);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }

  const realDist = realpathSync(options.dist);
  const realKey = realpathSync(options.key);
  const keyFromDist = relative(realDist, realKey);
  if (keyFromDist === "" || (!keyFromDist.startsWith(`..${sep}`) && keyFromDist !== ".." && !isAbsolute(keyFromDist))) {
    throw new Error("개인 키는 dist 디렉터리 밖에 두어야 해요.");
  }

  readProvenance(options.dist, options.nativeVersion);
  const privateKeyIdentity = statSync(realKey);
  const privateKeyContent = readFileSync(realKey);
  const privateKey = loadPrivateKey(privateKeyContent);
  const entries = collectEntries(options.dist, privateKeyIdentity, privateKeyContent);
  const zip = buildZip(entries);
  if (zip.length > MAX_DOWNLOAD_BYTES) throw new Error("OTA ZIP은 20MiB를 넘을 수 없어요.");
  const payload = {
    schema: 1,
    version: options.version,
    sequence: options.sequence,
    nativeVersion: options.nativeVersion,
    url: options.url,
    sha256: createHash("sha256").update(zip).digest("hex"),
  };
  const payloadBytes = Buffer.from(JSON.stringify(payload), "utf8");
  const manifest = {
    payload: payloadBytes.toString("base64"),
    signature: sign("RSA-SHA256", payloadBytes, privateKey).toString("base64"),
  };
  const publicKey = createPublicKey(privateKey).export({ type: "spki", format: "der" }).toString("base64");

  mkdirSync(dirname(options.out), { recursive: true });
  const temporary = `${options.out}.tmp-${process.pid}-${Date.now()}`;
  try {
    mkdirSync(temporary);
    writeFileSync(join(temporary, "bundle.zip"), zip, { flag: "wx" });
    writeFileSync(join(temporary, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, { flag: "wx" });
    writeFileSync(join(temporary, "public-key.txt"), `${publicKey}\n`, { flag: "wx" });
    renameSync(temporary, options.out);
  } catch (error) {
    rmSync(temporary, { recursive: true, force: true });
    throw error;
  }
}

try {
  const options = parseArguments(process.argv.slice(2));
  build(options);
  console.log(`OTA 산출물: ${options.out}`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
