import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_INTERVAL_MS = 1_000;

function executeAdb(args) {
  const result = spawnSync("adb", args, {
    encoding: "utf8",
    timeout: 10_000,
  });
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error((result.stderr || result.stdout || `adb exited with ${result.status}`).trim());
  }
  return result.stdout;
}

function delay(milliseconds) {
  return new Promise((complete) => setTimeout(complete, milliseconds));
}

function connectedDevices(output) {
  return output
    .split(/\r?\n/)
    .slice(1)
    .filter((line) => /\tdevice$/.test(line))
    .map((line) => line.split("\t", 1)[0]);
}

export async function resolveEmulatorSerial({
  adb = executeAdb,
  requestedSerial = process.env.ANDROID_SERIAL,
} = {}) {
  const devices = connectedDevices(await adb(["devices"]));
  const serial = requestedSerial || (devices.length === 1 ? devices[0] : "");
  if (!serial || !devices.includes(serial)) {
    throw new Error(
      requestedSerial
        ? `ANDROID_SERIAL=${requestedSerial} 에 연결된 에뮬레이터가 없어요.`
        : `연결된 에뮬레이터를 하나만 준비해 주세요. 현재 연결된 기기: ${devices.length}대`,
    );
  }
  if (!serial.startsWith("emulator-")) {
    throw new Error(`기기 ${serial} 는 에뮬레이터가 아니에요. 화면 상태를 변경하지 않습니다.`);
  }
  return serial;
}

function isAwake(power) {
  return /mWakefulness\s*[=:]\s*Awake\b/i.test(power);
}

function isKeyguardClear(window) {
  const keyguardShowing = /(?:isKeyguardShowing|mKeyguardShowing|mShowingLockscreen)\s*[=:]\s*true\b/i.test(window);
  const keyguardCleared = /(?:isKeyguardShowing|mKeyguardShowing|mShowingLockscreen)\s*[=:]\s*false\b/i.test(window);
  return keyguardCleared && !keyguardShowing;
}

export async function waitForEmulatorReady({
  serial,
  adb = executeAdb,
  sleep = delay,
  now = Date.now,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  intervalMs = DEFAULT_INTERVAL_MS,
} = {}) {
  if (!serial?.startsWith("emulator-")) {
    throw new Error(`에뮬레이터 serial 이 필요해요: ${serial || "없음"}`);
  }

  const startedAt = now();
  let attempts = 0;
  let power = "(조회하지 못함)";
  let window = "(조회하지 못함)";
  let adbError = "";

  while (true) {
    attempts += 1;
    try {
      await adb(["-s", serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"]);
      await adb(["-s", serial, "shell", "svc", "power", "stayon", "true"]);
      await adb(["-s", serial, "shell", "wm", "dismiss-keyguard"]);
      await adb(["-s", serial, "shell", "input", "keyevent", "82"]);
      power = await adb(["-s", serial, "shell", "dumpsys", "power"]);
      window = await adb(["-s", serial, "shell", "dumpsys", "window"]);
      adbError = "";
      if (isAwake(power) && isKeyguardClear(window)) {
        return { serial, attempts, power, window };
      }
    } catch (error) {
      adbError = error instanceof Error ? error.message : String(error);
    }

    if (now() - startedAt >= timeoutMs) {
      throw new Error([
        `${serial} 화면이 ${timeoutMs}ms 안에 준비되지 않았어요 (${attempts}회 확인).`,
        adbError ? `adb 오류:\n${adbError}` : "",
        `power 상태:\n${power.trim()}`,
        `window 상태:\n${window.trim()}`,
      ].filter(Boolean).join("\n"));
    }
    await sleep(intervalMs);
  }
}

async function main() {
  const serial = await resolveEmulatorSerial();
  const ready = await waitForEmulatorReady({ serial });
  console.log(`${ready.serial} 화면 준비 완료 (${ready.attempts}회 확인)`);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  });
}
