import { afterEach, describe, expect, it, vi } from "vitest";

// Only the "reports a 409 from a failed push registration" test below flips this to true; every other
// test needs isNative() === false to exercise the browser-storage fallback, same as the real Capacitor plugin off-device.
const nativePlatform = { active: false };

vi.mock("@capacitor/core", () => ({
  Capacitor: { isNativePlatform: () => nativePlatform.active },
  registerPlugin: () => ({
    read: vi.fn(),
    write: vi.fn(),
    clear: vi.fn(),
    pushConfigured: vi.fn().mockResolvedValue({ configured: true }),
    openNotificationSettings: vi.fn().mockResolvedValue(undefined),
  }),
}));

vi.mock("@capacitor/app", () => ({
  App: { addListener: vi.fn().mockResolvedValue({ remove: vi.fn() }) },
}));

vi.mock("@capacitor/push-notifications", () => {
  let registrationHandler: (token: { value: string }) => void | Promise<void> = () => undefined;
  return {
    PushNotifications: {
      checkPermissions: vi.fn().mockResolvedValue({ receive: "granted" }),
      requestPermissions: vi.fn().mockResolvedValue({ receive: "granted" }),
      addListener: vi.fn(async (event: string, handler: (token: { value: string }) => void | Promise<void>) => {
        if (event === "registration") registrationHandler = handler;
        return { remove: vi.fn() };
      }),
      // Fires the "registration" listener synchronously, standing in for the native FCM callback.
      register: vi.fn(async () => {
        await registrationHandler({ value: "token-abc" });
      }),
    },
  };
});

import { ApiError } from "./api";
import {
  awaitPushRegistration,
  clearToken,
  initializePlatform,
  readToken,
  writeToken,
} from "./platform";

describe("browser session storage", () => {
  afterEach(async () => {
    await clearToken();
    vi.unstubAllGlobals();
  });

  it("keeps a browser token only in process memory", async () => {
    const persistedValues = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (key: string) => persistedValues.get(key) ?? null,
        setItem: (key: string, value: string) => persistedValues.set(key, value),
      },
      sessionStorage: {
        getItem: (key: string) => persistedValues.get(key) ?? null,
        setItem: (key: string, value: string) => persistedValues.set(key, value),
      },
    });
    await writeToken("session-token");

    await expect(readToken()).resolves.toBe("session-token");
    expect(window.localStorage.getItem("jari.session")).toBeNull();
    expect(window.sessionStorage.getItem("jari.session")).toBeNull();
  });

  it("removes the browser token when cleared", async () => {
    await writeToken("session-token");
    await clearToken();

    await expect(readToken()).resolves.toBeNull();
  });
});

describe("push registration", () => {
  it("waits until the token has been saved before completing", async () => {
    let finishSaving: (saved: boolean) => void = () => undefined;
    const saved = new Promise<boolean>((resolve) => {
      finishSaving = resolve;
    });
    let completed = false;
    const registration = awaitPushRegistration(async () => undefined, saved).then((result) => {
      completed = true;
      return result;
    });

    await Promise.resolve();
    expect(completed).toBe(false);

    finishSaving(true);
    await expect(registration).resolves.toBe(true);
  });

  it("passes an ApiError's own message through onPushError instead of a generic one", async () => {
    nativePlatform.active = true;
    const onPushError = vi.fn();

    await initializePlatform({
      enablePushRegistration: true,
      onPushToken: async () => {
        // The server's 409 when this account already has as many devices as it's allowed.
        throw new ApiError("등록할 수 있는 기기 수를 넘었어요.", 409, "server");
      },
      onPushError,
    });

    expect(onPushError).toHaveBeenCalledWith("등록할 수 있는 기기 수를 넘었어요.");
  });

  it.each([
    new ApiError("이전 로그인에서 시작한 요청이라 중단했어요.", 0, "stale"),
    new ApiError("로그인이 필요해요.", 401, "auth"),
  ])("keeps the generic message for a $kind ApiError instead of its own", async (error) => {
    nativePlatform.active = true;
    const onPushError = vi.fn();

    await initializePlatform({
      enablePushRegistration: true,
      onPushToken: async () => {
        throw error;
      },
      onPushError,
    });

    expect(onPushError).toHaveBeenCalledWith("휴대폰 알림을 서버에 등록하지 못했어요.");
  });

  afterEach(() => {
    nativePlatform.active = false;
  });
});
