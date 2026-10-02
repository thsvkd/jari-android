#!/usr/bin/env node
// 전체 검증. 에뮬레이터 기기 단계까지 모두 통과하면 지금 스테이징된 내용(트리 해시)을 기기 검증 기록에 남기고,
// 릴리스 태그(v*)를 푸시할 때 .githooks/pre-push 가 그 기록을 봐요. 커밋 훅은 빠른 검사만 해요.
//
//   npm run verify            에뮬레이터 e2e → 유닛·모듈 → 통합(실서버+가짜 코레일) → 헤드리스 e2e·레이아웃·스크린샷 → 기록
//   npm run verify -- --ci    기기 없이(GitHub Actions). 기록은 남기지 않아요.
//   npm run verify -- --ci --device --only=에뮬레이터   GitHub Actions 의 Android 에뮬레이터로 기기 단계만.
//   npm run verify -- --only=에뮬레이터   이름에 그 말이 든 단계만(고치는 동안). 기록은 남기지 않아요.
//
// 실제 코레일에는 닿지 않아요. 기기 단계는 에뮬레이터에 평소 앱(dev.thsvkd.jari, 디버그 빌드)을 그대로 깔고 앱 데이터를 비운 채 돌려요.
// 그래서 실폰에서는 거부해요(폰의 실제 앱을 덮어쓰고 지워요). 정말 폰에서 돌릴 때만 JARI_ALLOW_PHYSICAL_DEVICE=1 을 줘요.

import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const BACKEND = join(ROOT, "backend");
const CI = process.argv.includes("--ci");
// CI 에서도 기기(에뮬레이터)가 붙어 있으면 기기 단계를 돌려요.
const DEVICE = !CI || process.argv.includes("--device");
const ONLY = process.argv.find((arg) => arg.startsWith("--only="))?.slice("--only=".length);
const E2E_API_PORT = 18281; // playwright.config.ts 의 E2E.api
const DEVTOOLS_PORT = 9377; // 이 PC 의 9222 는 다른 프로그램이 써요.
// 설치되는 패키지(applicationId)와 액티비티 클래스(코드의 namespace)는 달라요. Play 에서 com.jari.app 을 쓸 수 없어 패키지만 바꿨어요.
const APP = "dev.thsvkd.jari";
const ACTIVITY = "com.jari.app.MainActivity";

// macOS(brew)에서는 JDK 21 과 Android SDK 위치를 따로 알려 주지 않아도 기기 단계가 APK 를 빌드할 수 있게 해요.
if (process.platform === "darwin") {
  const jdk = "/opt/homebrew/opt/openjdk@21";
  const sdk = "/opt/homebrew/share/android-commandlinetools";
  if (!process.env.JAVA_HOME && existsSync(jdk)) process.env.JAVA_HOME = jdk;
  if (!process.env.ANDROID_HOME && existsSync(sdk)) process.env.ANDROID_HOME = sdk;
  if (process.env.JAVA_HOME) process.env.PATH = `${process.env.JAVA_HOME}/bin:${process.env.PATH}`;
}

function run(command, { cwd = ROOT, env = {}, capture = false, timeout } = {}) {
  const result = spawnSync(command, {
    cwd,
    env: { ...process.env, ...env },
    shell: true,
    stdio: capture ? ["ignore", "pipe", "pipe"] : "inherit",
    encoding: "utf8",
    timeout,
  });
  if (result.status !== 0) {
    const detail = capture ? `\n${result.stdout}${result.stderr}` : "";
    throw new Error(`실패: ${command}${detail}`);
  }
  return capture ? result.stdout.trim() : "";
}

const git = (args) => run(`git ${args}`, { capture: true });
const adb = (args) => run(`adb ${args}`, { capture: true });

// 기록이 뜻을 가지려면 검증한 파일이 곧 커밋할 파일이어야 해요.
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
        : `연결된 기기가 ${devices.length}대예요. 에뮬레이터 한 대만 띄우거나, 여러 대면 ANDROID_SERIAL 로 하나를 골라 주세요.`,
    );
  }
  // 이 단계는 평소 앱(dev.thsvkd.jari)을 덮어쓰고 데이터를 지워요. 실폰의 실제 앱·로그인을 지키려고 에뮬레이터에서만 돌려요.
  const target = serial || devices[0].split("\t")[0];
  if (!target.startsWith("emulator-") && process.env.JARI_ALLOW_PHYSICAL_DEVICE !== "1") {
    throw new Error(
      `기기 ${target} 는 에뮬레이터가 아니에요. 기기 검증은 평소 앱(${APP})을 덮어쓰고 앱 데이터를 지워서, 실폰의 실제 앱과 로그인이 사라져요. 에뮬레이터(emulator-…)를 띄워 ANDROID_SERIAL 로 고르거나, 폰의 앱을 지워도 좋다면 JARI_ALLOW_PHYSICAL_DEVICE=1 을 주세요.`,
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
  // 로컬 서버 주소로 웹 번들 → Capacitor 동기화 → 디버그 APK(평소 앱과 같은 dev.thsvkd.jari). 푸시(Firebase)는 빼서 알림 권한·토큰 등록이 끼지 않게 해요.
  run("npm run build", { env: { VITE_API_BASE_URL: `http://127.0.0.1:${E2E_API_PORT}` } });
  run("npx cap sync android");
  run(`${process.platform === "win32" ? "gradlew.bat" : "./gradlew"} assembleDebug -PjariNoFirebase`, { cwd: join(ROOT, "android") });
  const apk = join(ROOT, "android/app/build/outputs/apk/debug/app-debug.apk");
  try {
    adb(`install -r "${apk}"`);
  } catch (error) {
    // 에뮬레이터에 릴리스 서명으로 깐 앱이 남아 있으면 서명이 달라 덮어쓸 수 없어요. 에뮬레이터니까 지우고 다시 깔아요.
    if (!String(error.message).includes("INSTALL_FAILED_UPDATE_INCOMPATIBLE")) throw error;
    spawnSync(`adb uninstall ${APP}`, { shell: true });
    adb(`install "${apk}"`);
  }
  // 앞선 실행의 로그인·설정이 남지 않게 앱 데이터를 비워요. 테스트 사이의 로그아웃은 e2e/fixtures.ts 의 openApp 이 해요.
  adb(`shell pm clear ${APP}`);
  adb(`reverse tcp:${E2E_API_PORT} tcp:${E2E_API_PORT}`);
  adb(`shell am force-stop ${APP}`);
  adb(`shell am start -n ${APP}/${ACTIVITY}`);
  let pid = "";
  for (let attempt = 0; attempt < 30 && !pid; attempt++) {
    pid = spawnSync(`adb shell pidof ${APP}`, { shell: true, encoding: "utf8" }).stdout.trim();
    if (!pid) spawnSync(process.execPath, ["-e", "setTimeout(() => {}, 500)"]);
  }
  if (!pid) throw new Error("앱이 시작되지 않았어요.");
  // WebView 디버깅 소켓은 앱이 WebView 를 만든 뒤에 생겨요.
  let socket = "";
  for (let attempt = 0; attempt < 30 && !socket; attempt++) {
    socket = adb("shell cat /proc/net/unix").split("\n").map((line) => line.trim().split(" ").pop())
      .find((name) => name === `@webview_devtools_remote_${pid}`) ?? "";
    if (!socket) spawnSync(process.execPath, ["-e", "setTimeout(() => {}, 500)"]);
  }
  if (!socket) throw new Error("앱의 WebView 디버깅 소켓을 찾지 못했어요.");
  adb(`forward tcp:${DEVTOOLS_PORT} localabstract:${socket.slice(1)}`);
  const top = adb(`shell "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity:'"`);
  if (!top.includes(APP)) throw new Error(`전면 앱이 ${APP} 가 아니에요. 기기를 잠시 그대로 두세요.\n${top}`);
  return `http://127.0.0.1:${DEVTOOLS_PORT}`;
}

function releaseDevice() {
  // adb 가 응답하지 않으면 제한 없이 기다려 기기 잡(45분)이 실패 대신 취소로 끝나요.
  for (const command of [
    `adb forward --remove tcp:${DEVTOOLS_PORT}`,
    `adb reverse --remove tcp:${E2E_API_PORT}`,
  ]) {
    spawnSync(command, { shell: true, timeout: 15_000 });
  }
}

// 기기 단계를 맨 앞에 둬요. 에뮬레이터를 먼저 띄워 두면 되고, 나머지는 기기 없이 돌아요.
const deviceStep = [
  "에뮬레이터 e2e (dev.thsvkd.jari)",
  () => {
    const cdp = prepareDevice();
    try {
      // 결과 출력 뒤에 프로세스가 멈춰도 잡 제한까지 가지 않게 해요. 통과한 기기 단계는 15분을 넘기지 않아요.
      run("npx playwright test --project=device", { env: { JARI_DEVICE_CDP: cdp }, timeout: 25 * 60 * 1000 });
    } finally {
      releaseDevice();
    }
  },
];

const steps = [
  ...(DEVICE ? [deviceStep] : []),
  ["프론트 유닛·모듈 (vitest)", () => run("npm test")],
  ["프론트 타입·빌드", () => run("npm run build && npx tsc -p e2e/tsconfig.json --noEmit && npx tsc -p monitor/tsconfig.json --noEmit")],
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
  // 몇 분을 돌리고 나서 기록을 못 남긴다고 알리는 것보다 지금 멈추는 게 나아요.
  console.error(`기기 검증 기록을 남길 수 없는 상태예요.\n${before.reason}\n테스트만 돌리려면 npm run verify -- --ci`);
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

// 검증 중에 파일이 바뀌었다면 그 기록은 거짓이에요.
const after = stagedTree();
if (!before.tree || before.tree !== after.tree) {
  console.error(`\n기기 검증 기록은 남기지 않았어요. ${before.reason || after.reason || "검증 중에 스테이징된 내용이 바뀌었어요."}`);
  process.exit(1);
}
// 에뮬레이터 기기 단계까지 통과한 트리는 따로 쌓아 둬요. 릴리스 태그(v*)를 푸시할 때 .githooks/pre-push 가 이 목록을 봐요.
// 워크트리끼리 같이 쓰도록 공용 git 디렉터리에 둬요.
const deviceList = resolve(ROOT, git("rev-parse --git-common-dir"), "jari-device-verified");
appendFileSync(deviceList, `${after.tree} ${new Date().toISOString()}\n`);
console.log(`\n기기 검증 기록: ${after.tree} (${deviceList})`);
