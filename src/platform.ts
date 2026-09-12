import { Capacitor, registerPlugin } from "@capacitor/core";

interface SecureSessionPlugin {
  read(): Promise<{ token: string | null }>;
  write(options: { token: string }): Promise<void>;
  clear(): Promise<void>;
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
    options.onPushError?.("푸시 토큰을 서버에 등록할 설정이 없습니다.");
    return { native: true, push: "unavailable" };
  }

  const { PushNotifications } = await import("@capacitor/push-notifications");
  const permissions = await PushNotifications.checkPermissions();
  const permission =
    permissions.receive === "prompt"
      ? await PushNotifications.requestPermissions()
      : permissions;

  if (permission.receive !== "granted") {
    options.onPushError?.("알림 권한이 허용되지 않았습니다.");
    return { native: true, push: "unavailable" };
  }

  const registered = await PushNotifications.addListener("registration", async (token) => {
    try {
      await options.onPushToken?.(token.value);
    } catch {
      options.onPushError?.("푸시 토큰을 서버에 등록하지 못했습니다.");
    }
  });
  const registrationError = await PushNotifications.addListener(
    "registrationError",
    (error) => options.onPushError?.(error.error),
  );
  removePushListeners = [
    () => registered.remove(),
    () => registrationError.remove(),
  ];

  try {
    await PushNotifications.register();
    return { native: true, push: "requested" };
  } catch {
    options.onPushError?.("Firebase 설정이 없어 푸시 등록을 시작할 수 없습니다.");
    await Promise.all(removePushListeners.map((remove) => remove()));
    removePushListeners = [];
    return { native: true, push: "unavailable" };
  }
}
