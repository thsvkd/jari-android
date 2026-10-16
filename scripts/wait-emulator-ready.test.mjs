import { describe, expect, test, vi } from "vitest";

import { resolveEmulatorSerial, waitForEmulatorReady } from "./wait-emulator-ready.mjs";

function fakeClock() {
  let current = 0;
  return {
    now: () => current,
    sleep: async (milliseconds) => {
      current += milliseconds;
    },
  };
}

describe("waitForEmulatorReady", () => {
  test("화면 상태 전환이 늦으면 Awake 및 키가드 해제를 확인할 때까지 기다린다", async () => {
    const clock = fakeClock();
    let attempt = 0;
    const adb = vi.fn(async (args) => {
      const command = args.join(" ");
      if (command.endsWith("shell dumpsys power")) {
        return attempt === 0 ? "mWakefulness=Asleep" : "mWakefulness=Awake";
      }
      if (command.endsWith("shell dumpsys window")) {
        const result = attempt === 0 ? "isKeyguardShowing=true" : "isKeyguardShowing=false";
        attempt += 1;
        return result;
      }
      return "";
    });

    const result = await waitForEmulatorReady({
      serial: "emulator-5554",
      adb,
      now: clock.now,
      sleep: clock.sleep,
      intervalMs: 100,
      timeoutMs: 1_000,
    });

    expect(result).toMatchObject({ serial: "emulator-5554", attempts: 2 });
    expect(result.power).toContain("mWakefulness=Awake");
    expect(result.window).toContain("isKeyguardShowing=false");
  });

  test("Awake여도 키가드가 계속 보이면 상태 진단과 함께 실패한다", async () => {
    const clock = fakeClock();
    const adb = vi.fn(async (args) => {
      const command = args.join(" ");
      if (command.endsWith("shell dumpsys power")) return "mWakefulness=Awake";
      if (command.endsWith("shell dumpsys window")) return "isKeyguardShowing=true";
      return "";
    });

    await expect(waitForEmulatorReady({
      serial: "emulator-5554",
      adb,
      now: clock.now,
      sleep: clock.sleep,
      intervalMs: 100,
      timeoutMs: 200,
    })).rejects.toThrow(/isKeyguardShowing=true/);
  });

  test("키가드 상태가 출력에 없으면 해제됐다고 간주하지 않는다", async () => {
    const clock = fakeClock();
    const adb = vi.fn(async (args) => {
      const command = args.join(" ");
      if (command.endsWith("shell dumpsys power")) return "mWakefulness=Awake";
      if (command.endsWith("shell dumpsys window")) return "WindowManager state unavailable";
      return "";
    });

    await expect(waitForEmulatorReady({
      serial: "emulator-5554",
      adb,
      now: clock.now,
      sleep: clock.sleep,
      intervalMs: 100,
      timeoutMs: 100,
    })).rejects.toThrow(/WindowManager state unavailable/);
  });

  test("adb 상태 조회가 실패하면 준비 완료로 오인하지 않고 오류를 남긴다", async () => {
    const clock = fakeClock();
    const adb = vi.fn(async () => {
      throw new Error("adb: device offline");
    });

    await expect(waitForEmulatorReady({
      serial: "emulator-5554",
      adb,
      now: clock.now,
      sleep: clock.sleep,
      intervalMs: 100,
      timeoutMs: 100,
    })).rejects.toThrow(/adb: device offline/);
  });
});

describe("resolveEmulatorSerial", () => {
  test("연결된 기기가 없으면 화면 조작 전에 실패한다", async () => {
    const adb = vi.fn(async () => "List of devices attached\n\n");

    await expect(resolveEmulatorSerial({ adb, requestedSerial: "" })).rejects.toThrow(/연결된 에뮬레이터/);
    expect(adb).toHaveBeenCalledTimes(1);
  });

  test("실물 기기는 화면 조작 전에 거부한다", async () => {
    const adb = vi.fn(async () => "List of devices attached\nR3CT123456\tdevice\n");

    await expect(resolveEmulatorSerial({ adb, requestedSerial: "" })).rejects.toThrow(/에뮬레이터가 아니에요/);
    expect(adb).toHaveBeenCalledTimes(1);
  });
});
