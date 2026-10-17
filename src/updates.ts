import { Capacitor, registerPlugin } from "@capacitor/core";

export interface UpdateStatus {
  bootId: string;
  supported: boolean;
  currentVersion: string;
  nativeVersion: string;
}

export type UpdateCheck = { status: "current" | "available" | "incompatible"; version?: string };

/** The native side owns the verified candidate; JS cannot select a path or a download URL. */
export interface AppUpdatesClient {
  getStatus(): Promise<UpdateStatus>;
  check(): Promise<UpdateCheck>;
  apply(): Promise<void>;
  ready(options: { bootId: string }): Promise<void>;
}

const AppUpdates = registerPlugin<AppUpdatesClient>("AppUpdates");

export function nativeUpdates(): AppUpdatesClient | undefined {
  return Capacitor.isPluginAvailable("AppUpdates") ? AppUpdates : undefined;
}
