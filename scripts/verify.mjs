#!/usr/bin/env node
// 커밋 전 전체 검증. 모두 통과하면 지금 스테이징된 내용(트리 해시)에 도장을 찍고, pre-commit 훅은 그 도장이 있어야 커밋을 받아요.
//
//   npm run verify            실기기 e2e → 유닛·모듈 → 통합(실서버+가짜 코레일) → 헤드리스 e2e·레이아웃·스크린샷 → 도장
//   npm run verify -- --ci    기기 없이(GitHub Actions). 도장은 찍지 않아요.
//   npm run verify -- --only=실기기   이름에 그 말이 든 단계만(고치는 동안). 도장은 찍지 않아요.
//
// 실제 코레일에는 닿지 않아요. 실기기 단계는 com.jari.app.e2e 를 따로 설치해 실제 앱의 로그인·데이터를 건드리지 않아요.

import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const BACKEND = join(ROOT, "backend");
const CI = process.argv.includes("--ci");
const ONLY = process.argv.find((arg) => arg.startsWith("--only="))?.slice("--only=".length);
const E2E_API_PORT = 18281; // playwright.config.ts 의 E2E.api
const DEVTOOLS_PORT = 9377; // 이 PC 의 9222 는 다른 프로그램이 써요.
const E2E_APP = "com.jari.app.e2e";

function run(command, { cwd = ROOT, env = {}, capture = false } = {}) {
  const result = spawnSync(command, {
    cwd,
    env: { ...process.env, ...env },
    shell: true,
    stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit",
    encoding: "utf8",
  });
  if (result.status !== 0) {
    const detail = capture ? `\n${result.stdout}${result.stderr}` : "";
    throw new Error(`실패: ${command}${detail}`);
  }
  return capture ? result.stdout.trim() : "";
}

const git = (args) => run(`git ${args}`, { capture: true });
const adb = (args) => run(`adb ${args}`, { capture: true });

// 도장이 뜻을 가지려면 검증한 파일이 곧 커밋할 파일이어야 해요.
function stagedTree() {
  const unstaged = spawnSync("git diff --quiet", { cwd: ROOT, shell: true }).status !== 0;
  const untracked = git("ls-files --others --exclude-standard");
  if (unstaged || untracked) {
    return {
      tree: null,
      reason: [
        unstaged ? "스테이징하지 않은 변경이 있어요(git add 해 주세요)" : "",
        untracked ? `추적하지 않는 파일이 있어요:\n${untracked}` : "",
      ].filter(Boolean).join("\n"),
    };
  }
  return { tree: git("write-tree"), reason: "" };
}

function prepareDevice() {
  // Windows 의 adb 는 줄 끝이 \r\n 이에요. \n 으로만 자르면 마지막 줄(trim 된 한 줄)만 기기로 잡혀요.
  const devices = adb("devices").split(/\r?\n/).slice(1).filter((line) => /\tdevice$/.test(line));
  // ANDROID_SERIAL 을 주면 adb 가 모든 명령을 그 기기로 보내요. 폰과 에뮬레이터가 함께 붙어 있어도 하나를 골라 돌릴 수 있어요.
  const serial = process.env.ANDROID_SERIAL;
  if (serial ? !devices.some((line) => line.startsWith(`${serial}\t`)) : devices.length !== 1) {
    throw new Error(
      serial
        ? `ANDROID_SERIAL=${serial} 기기가 연결돼 있지 않아요. \`adb devices\` 로 이름을 확인해 주세요.`
        : `연결된 기기가 ${devices.length}대예요. 실기기 한 대를 연결하거나, 여러 대면 ANDROID_SERIAL 로 하나를 골라 주세요(무선 디버깅이면 \`! adb connect <IP>:<포트>\`).`,
    );
  }
  // 잠긴 화면·꺼진 화면에서는 WebView 가 그리지 않고 타이머도 늦어져요. 잠금은 풀지 않고(보안 설정이에요) 사람에게 부탁해요.
  const power = adb(`shell "dumpsys power | grep mWakefulness="`);
  const locked = adb(`shell "dumpsys window | grep isKeyguardShowing"`);
  if (!power.includes("Awake") || locked.includes("isKeyguardShowing=true")) {
    throw new Error(
      "기기 화면이 꺼져 있거나 잠겨 있어요. 잠금을 풀고 검증이 끝날 때까지 화면을 켜 두세요(개발자 옵션의 '화면 켜진 상태로 유지'가 편해요).",
    );
  }
  run(`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-android.ps1 -E2ePort ${E2E_API_PORT}`);
  run(`adb install -r "${join(ROOT, "android/app/build/outputs/apk/e2e/app-e2e.apk")}"`);
  adb(`reverse tcp:${E2E_API_PORT} tcp:${E2E_API_PORT}`);
  adb(`shell am force-stop ${E2E_APP}`);
  adb(`shell am start -n ${E2E_APP}/com.jari.app.MainActivity`);
  let pid = "";
  for (let attempt = 0; attempt < 30 && !pid; attempt++) {
    pid = spawnSync(`adb shell pidof ${E2E_APP}`, { shell: true, encoding: "utf8" }).stdout.trim();
    if (!pid) spawnSync(process.execPath, ["-e", "setTimeout(() => {}, 500)"]);
  }
  if (!pid) throw new Error("e2e 앱이 시작되지 않았어요.");
  // WebView 디버깅 소켓은 앱이 WebView 를 만든 뒤에 생겨요.
  let socket = "";
  for (let attempt = 0; attempt < 30 && !socket; attempt++) {
    socket = adb("shell cat /proc/net/unix").split("\n").map((line) => line.trim().split(" ").pop())
      .find((name) => name === `@webview_devtools_remote_${pid}`) ?? "";
    if (!socket) spawnSync(process.execPath, ["-e", "setTimeout(() => {}, 500)"]);
  }
  if (!socket) throw new Error("e2e 앱의 WebView 디버깅 소켓을 찾지 못했어요.");
  adb(`forward tcp:${DEVTOOLS_PORT} localabstract:${socket.slice(1)}`);
  const top = adb(`shell "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity:'"`);
  if (!top.includes(E2E_APP)) throw new Error(`전면 앱이 ${E2E_APP} 가 아니에요. 기기를 잠시 그대로 두세요.\n${top}`);
  return `http://127.0.0.1:${DEVTOOLS_PORT}`;
}

function releaseDevice() {
  spawnSync(`adb forward --remove tcp:${DEVTOOLS_PORT}`, { shell: true });
  spawnSync(`adb reverse --remove tcp:${E2E_API_PORT}`, { shell: true });
}

// 실기기 단계를 맨 앞에 둬요. 폰 잠금을 풀고 시작하면 처음 몇 분만 화면이 켜져 있으면 되고, 나머지는 폰 없이 돌아요.
const deviceStep = [
  "실기기 e2e (com.jari.app.e2e)",
  () => {
    const cdp = prepareDevice();
    try {
      run("npx playwright test --project=device", { env: { JARI_DEVICE_CDP: cdp } });
    } finally {
      releaseDevice();
    }
  },
];

const steps = [
  ...(CI ? [] : [deviceStep]),
  ["프론트 유닛·모듈 (vitest)", () => run("npm test")],
  ["프론트 타입·빌드", () => run("npm run build && npx tsc -p e2e/tsconfig.json --noEmit")],
  ["데모 빌드", () => run("npm run build:demo")],
  ...(process.platform === "win32"
    ? [["Android 빌드 스크립트", () => run("powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test-build-android.ps1")]]
    : []),
  ["백엔드 린트", () => run("uv run --frozen ruff check src tests && uv run --frozen ruff format --check src tests", { cwd: BACKEND })],
  ["백엔드 유닛·모듈 (pytest)", () => run("uv run --frozen pytest tests/unit -q", { cwd: BACKEND })],
  ["통합: 실서버 + 가짜 코레일 (pytest)", () => run("uv run --frozen pytest tests/e2e -q", { cwd: BACKEND })],
  ["e2e·레이아웃·스크린샷: 헤드리스", () => run("npx playwright test --project=phone-light --project=phone-dark --project=phone-narrow")],
];

const partial = CI || Boolean(ONLY);
const before = partial ? null : stagedTree();
if (before && !before.tree) {
  // 몇 분을 돌리고 나서 도장을 못 찍는다고 알리는 것보다 지금 멈추는 게 나아요.
  console.error(`도장을 찍을 수 없는 상태예요.\n${before.reason}\n테스트만 돌리려면 npm run verify -- --ci`);
  process.exit(1);
}
const timings = [];
for (const [name, step] of steps.filter(([name]) => !ONLY || name.includes(ONLY))) {
  const started = Date.now();
  console.log(`\n━━ ${name}`);
  try {
    step();
  } catch (error) {
    console.error(`\n✗ ${name}\n${error.message}`);
    process.exit(1);
  }
  timings.push(`${name}: ${Math.round((Date.now() - started) / 1000)}초`);
}

console.log(`\n✓ 전체 검증 통과\n${timings.map((line) => `  ${line}`).join("\n")}`);
if (partial) process.exit(0);

// 검증 중에 파일이 바뀌었다면 그 도장은 거짓이에요.
const after = stagedTree();
if (!before.tree || before.tree !== after.tree) {
  console.error(`\n도장은 찍지 않았어요. ${before.reason || after.reason || "검증 중에 스테이징된 내용이 바뀌었어요."}`);
  process.exit(1);
}
const stamp = resolve(ROOT, git("rev-parse --git-dir"), "jari-verified");
writeFileSync(stamp, JSON.stringify({ tree: after.tree, at: new Date().toISOString(), steps: timings }, null, 2));
console.log(`\n도장: ${after.tree} (${existsSync(stamp) ? stamp : ""})`);
// 실기기까지 통과한 트리는 따로 쌓아 둬요. 릴리스 태그(v*)를 푸시할 때 .githooks/pre-push 가 이 목록을 봐요.
// 워크트리끼리 같이 쓰도록 공용 git 디렉터리에 둬요.
const deviceList = resolve(ROOT, git("rev-parse --git-common-dir"), "jari-device-verified");
appendFileSync(deviceList, `${after.tree} ${new Date().toISOString()}\n`);
console.log(`실기기 검증 기록: ${deviceList}`);
