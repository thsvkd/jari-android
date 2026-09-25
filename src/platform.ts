import { Capacitor, registerPlugin } from "@capacitor/core";
import { ApiError } from "./api";

interface SecureSessionPlugin {
  read(): Promise<{ token: string | null }>;
  write(options: { token: string }): Promise<void>;
  clear(): Promise<void>;
  pushConfigured(): Promise<{ configured: boolean }>;
  openNotificationSettings(): Promise<void>;
}

const SecureSession = registerPlugin<SecureSessionPlugin>("SecureSession");

export interface PlatformInitializeOptions {
  /** Return true when the application consumed the Android back action. */
  onBack?: () => boolean | Promise<boolean>;
  /** Persist a native FCM registration token through the authenticated API. */
  onPushToken?: (token: string) => Promise<void>;
  /** Surface an unavailable Firebase/permission state without pretending push works. */
  onPushError?: (message: string) => void;
  /** Push registration is opt-in and does nothing unless this is explicitly true. */
  enablePushRegistration?: boolean;
}

export interface PlatformInitialization {
  native: boolean;
  push: "disabled" | "unavailable" | "requested";
}

let browserToken: string | null = null;
let removeBackListener: (() => Promise<void>) | undefined;
let removePushListeners: Array<() => Promise<void>> = [];

export async function awaitPushRegistration(
  register: () => Promise<void>,
  registration: Promise<boolean>,
  timeoutMs = 15_000,
): Promise<boolean> {
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const timedOut = new Promise<boolean>((resolve) => {
    timeout = setTimeout(() => resolve(false), timeoutMs);
  });

  try {
    await register();
    return await Promise.race([registration, timedOut]);
  } finally {
    if (timeout) clearTimeout(timeout);
  }
}

function isNative(): boolean {
  return Capacitor.isNativePlatform();
}

export async function readToken(): Promise<string | null> {
  if (!isNative()) {
    return browserToken;
  }

  return (await SecureSession.read()).token;
}

export async function writeToken(token: string): Promise<void> {
  if (!token) {
    throw new Error("A session token is required");
  }

  if (!isNative()) {
    browserToken = token;
    return;
  }

  await SecureSession.write({ token });
}

export async function clearToken(): Promise<void> {
  browserToken = null;
  if (isNative()) {
    await SecureSession.clear();
  }
}

// The app's theme is its own toggle, not the system's, so the status bar icons follow it here:
// left to the OS they stay light and vanish against the light header. Edge-to-edge on Android 15
// ignores background colours, so only the icon style is set.
export async function applyStatusBarStyle(theme: "light" | "dark"): Promise<void> {
  if (!isNative()) return;
  try {
    const { StatusBar, Style } = await import("@capacitor/status-bar");
    await StatusBar.setStyle({ style: theme === "dark" ? Style.Dark : Style.Light });
  } catch {
    // Cosmetic; a missing plugin must not take the app down.
  }
}

export async function disposePlatform(): Promise<void> {
  await removeBackListener?.();
  removeBackListener = undefined;

  await Promise.all(removePushListeners.map((remove) => remove()));
  removePushListeners = [];
}

export async function initializePlatform(
  options: PlatformInitializeOptions = {},
): Promise<PlatformInitialization> {
  await disposePlatform();

  if (!isNative()) {
    return { native: false, push: "unavailable" };
  }

  const { App } = await import("@capacitor/app");
  const backListener = await App.addListener("backButton", async () => {
    if (await options.onBack?.()) {
      return;
    }

    if (window.history.length > 1) {
      window.history.back();
      return;
    }

    await App.exitApp();
  });
  removeBackListener = () => backListener.remove();

  if (!options.enablePushRegistration) {
    return { native: true, push: "disabled" };
  }

  if (!options.onPushToken) {
    options.onPushError?.("휴대폰 알림을 서버에 등록할 설정이 없어요.");
    return { native: true, push: "unavailable" };
  }

  const pushConfiguration = await SecureSession.pushConfigured();
  if (!pushConfiguration.configured) {
    options.onPushError?.("휴대폰 알림 설정이 빠진 앱이에요. 최신 설치 파일로 다시 설치해 주세요.");
    return { native: true, push: "unavailable" };
  }

  const { PushNotifications } = await import("@capacitor/push-notifications");
  const permissions = await PushNotifications.checkPermissions();
  const alreadyGranted = permissions.receive === "granted";
  const permission =
    permissions.receive === "prompt"
      ? await PushNotifications.requestPermissions()
      : permissions;

  if (permission.receive !== "granted") {
    options.onPushError?.("알림 권한이 꺼져 있어요. 휴대폰 설정에서 권한을 허용해 주세요.");
    return { native: true, push: "unavailable" };
  }

  let finishRegistration: (registered: boolean) => void = () => undefined;
  let registrationErrorReported = false;
  const registration = new Promise<boolean>((resolve) => {
    finishRegistration = resolve;
  });
  const registered = await PushNotifications.addListener("registration", async (token) => {
    try {
      await options.onPushToken?.(token.value);
      finishRegistration(true);
    } catch (error) {
      registrationErrorReported = true;
      // A 409 (등록할 수 있는 기기 수를 넘었어요) etc. carries a message worth showing. "stale"/"auth" already
      // drive their own login-expiry flow, so those two keep the generic message instead of doubling up on it.
      options.onPushError?.(
        error instanceof ApiError && error.kind !== "stale" && error.kind !== "auth"
          ? error.message
          : "휴대폰 알림을 서버에 등록하지 못했어요.",
      );
      finishRegistration(false);
    }
  });
  const registrationError = await PushNotifications.addListener(
    "registrationError",
    (error) => {
      registrationErrorReported = true;
      options.onPushError?.(error.error);
      finishRegistration(false);
    },
  );
  removePushListeners = [
    () => registered.remove(),
    () => registrationError.remove(),
  ];

  try {
    const persisted = await awaitPushRegistration(
      () => PushNotifications.register(),
      registration,
    );
    if (!persisted) {
      if (!registrationErrorReported) {
        options.onPushError?.("휴대폰 알림 등록이 늦어지고 있어요. 잠시 후 다시 시도해 주세요.");
      }
      await Promise.all(removePushListeners.map((remove) => remove()));
      removePushListeners = [];
      return { native: true, push: "unavailable" };
    }
    if (alreadyGranted) await SecureSession.openNotificationSettings();
    return { native: true, push: "requested" };
  } catch {
    options.onPushError?.("휴대폰 알림 서비스가 아직 설정되지 않았어요.");
    await Promise.all(removePushListeners.map((remove) => remove()));
    removePushListeners = [];
    return { native: true, push: "unavailable" };
  }
}

/**
 * Android 15 의 가장자리까지 그리기에서는 키보드가 올라와도 WebView 높이가 그대로라, 아래쪽 버튼이 키보드 뒤에 숨어요.
 * 보이는 높이가 줄어든 만큼 페이지 아래에 여백(--keyboard-inset)을 두어 끝까지 스크롤할 수 있게 하고,
 * 입력 칸을 누르면 그 칸이 키보드 위로 오게 해요.
 */
export function followKeyboard(root: HTMLElement): () => void {
  const viewport = window.visualViewport;
  if (!viewport) return () => undefined;
  const update = () => {
    const hidden = Math.max(0, Math.round(window.innerHeight - viewport.height - viewport.offsetTop));
    document.documentElement.style.setProperty("--keyboard-inset", `${hidden}px`);
  };
  const reveal = (event: FocusEvent) => {
    const field = event.target;
    if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) return;
    // 키보드가 다 올라온 뒤에 옮겨야 줄어든 높이를 기준으로 가운데에 와요.
    window.setTimeout(() => field.scrollIntoView({ block: "center" }), 300);
  };
  viewport.addEventListener("resize", update);
  root.addEventListener("focusin", reveal);
  update();
  return () => {
    viewport.removeEventListener("resize", update);
    root.removeEventListener("focusin", reveal);
  };
}
