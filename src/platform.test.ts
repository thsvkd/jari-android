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
  App: { addListener: vi.fn().mockResolvedValue({ remove: vi.fn() }), getLaunchUrl: vi.fn().mockResolvedValue(undefined) },
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
  googleHandoffFrom,
  initializePlatform,
  readToken,
  watchGoogleReturn,
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

it("only opens the Google authorization origin in an external browser", async () => {
  const { openGoogleBrowser } = await import("./platform");
  const open = vi.spyOn(window, "open").mockReturnValue({ opener: null } as unknown as Window);
  await expect(openGoogleBrowser("javascript:alert(1)")).rejects.toThrow();
  await expect(openGoogleBrowser("https://accounts.google.com.evil.example/auth")).rejects.toThrow();
  await openGoogleBrowser("https://accounts.google.com/o/oauth2/v2/auth?state=one-time");
  expect(open).toHaveBeenCalledWith("https://accounts.google.com/o/oauth2/v2/auth?state=one-time", "_blank");
  open.mockRestore();
});

const HANDOFF = "h".repeat(43);

it("reads a Google handoff only from the app's own return address", () => {
  expect(googleHandoffFrom(`dev.thsvkd.jari://google-auth?handoff=${HANDOFF}`)).toBe(HANDOFF);
  expect(googleHandoffFrom(`dev.thsvkd.jari://other?handoff=${HANDOFF}`)).toBeNull();
  expect(googleHandoffFrom(`https://google-auth/?handoff=${HANDOFF}`)).toBeNull();
  expect(googleHandoffFrom(`evil.app://google-auth?handoff=${HANDOFF}`)).toBeNull();
  expect(googleHandoffFrom("dev.thsvkd.jari://google-auth?handoff=short")).toBeNull();
  expect(googleHandoffFrom("dev.thsvkd.jari://google-auth?handoff=" + "x".repeat(40) + "%3Cscript%3E")).toBeNull();
  expect(googleHandoffFrom("not a url")).toBeNull();
  expect(googleHandoffFrom(`dev.thsvkd.jari://google-auth/?other=1&handoff=${HANDOFF}#x`)).toBe(HANDOFF);
});

it("reads the handoff where an old WebView's URL parser leaves the host of a custom scheme empty", () => {
  // Chromium before 130 (every Android 7 WebView) parsed dev.thsvkd.jari://google-auth as host "" and path "//google-auth".
  const Standard = URL;
  vi.stubGlobal("URL", class extends Standard { get host(): string { return ""; } get hostname(): string { return ""; } });
  try {
    expect(googleHandoffFrom(`dev.thsvkd.jari://google-auth?handoff=${HANDOFF}`)).toBe(HANDOFF);
  } finally {
    vi.unstubAllGlobals();
  }
});

it("hands the browser's return to the app while running and at a cold launch", async () => {
  const { App } = await import("@capacitor/app");
  const onHandoff = vi.fn();
  await watchGoogleReturn(onHandoff);
  expect(App.addListener).not.toHaveBeenCalledWith("appUrlOpen", expect.anything());

  nativePlatform.active = true;
  try {
    vi.mocked(App.getLaunchUrl).mockResolvedValueOnce({ url: `dev.thsvkd.jari://google-auth?handoff=${HANDOFF}` });
    await watchGoogleReturn(onHandoff);
    expect(onHandoff).toHaveBeenCalledWith(HANDOFF);
    const calls = vi.mocked(App.addListener).mock.calls as unknown as [string, (event: { url: string }) => void][];
    const listener = calls.find(([event]) => event === "appUrlOpen")![1];
    listener({ url: "https://jari.thsvkd.dev/privacy" });
    listener({ url: `dev.thsvkd.jari://google-auth?handoff=${"k".repeat(43)}` });
    expect(onHandoff.mock.calls).toEqual([[HANDOFF], ["k".repeat(43)]]);
  } finally {
    nativePlatform.active = false;
  }
});
