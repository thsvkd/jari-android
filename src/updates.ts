import { Capacitor, registerPlugin } from "@capacitor/core";

export interface UpdateStatus {
  supported: boolean;
  source: "play" | "apk" | "unsupported";
  currentVersion: string;
  versionCode: number;
  reason?: string;
}

export type UpdateCheck =
  | { status: "current" }
  | { status: "available"; version?: string; versionCode: number };

/** The native side owns the verified candidate; JS cannot select a path or a download URL. */
export interface AppUpdatesClient {
  getStatus(): Promise<UpdateStatus>;
  check(): Promise<UpdateCheck>;
  apply(): Promise<{ status: "cancelled" | "started" }>;
}

const AppUpdates = registerPlugin<AppUpdatesClient>("AppUpdates");

export function nativeUpdates(): AppUpdatesClient | undefined {
  return Capacitor.isPluginAvailable("AppUpdates") ? AppUpdates : undefined;
}
