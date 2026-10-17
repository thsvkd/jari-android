import { afterEach, expect, it, vi } from "vitest";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";
import type { AppUpdatesClient, UpdateCheck, UpdateStatus } from "./updates";

const apps: JariApp[] = [];
afterEach(() => {
  apps.splice(0).forEach((app) => app.dispose());
  document.body.replaceChildren();
  window.localStorage.clear();
});

function updates(overrides: Partial<AppUpdatesClient> = {}): AppUpdatesClient {
  return {
    getStatus: async () => ({ supported: true, source: "play", currentVersion: "4.22.0", versionCode: 42201 }),
    check: async () => ({ status: "available", version: "4.23.0", versionCode: 42300 }),
    apply: async () => ({ status: "started" }),
    ...overrides,
  };
}

async function mount(client?: AppUpdatesClient, demoMode = false) {
  const root = document.createElement("div");
  document.body.append(root);
  const app = new JariApp(root, createDemoApi(), { demoMode, updates: client });
  apps.push(app);
  await app.start(true);
  app.navigate("settings");
  return root;
}

const press = (root: HTMLElement, action: string) =>
  root.querySelector<HTMLButtonElement>(`[data-action='${action}']`)!.click();

it.each([
  ["play", "Google Play", "v4.22.0"],
  ["apk", "새 설치 파일", "v4.22.0"],
] as const)("shows the installed human version and %s update source", async (source, sourceLabel, version) => {
  const root = await mount(updates({
    getStatus: async () => ({ supported: true, source, currentVersion: "4.22.0", versionCode: 42201 }),
  }));
  await vi.waitFor(() => expect(root.textContent).toContain(sourceLabel));
  expect(root.textContent).toContain(version);
  expect(root.textContent).not.toContain("42201");
  expect(root.textContent).not.toContain("버전 코드");
  expect(root.textContent).not.toContain("화면 버전");
});

it("accepts a Play update without a version name when its version code is valid", async () => {
  const root = await mount(updates({ check: async () => ({ status: "available", versionCode: 42300 }) }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  expect(root.textContent).toContain("새 앱 버전");
  expect(root.textContent).not.toContain("42300");
});

it.each([0, -1, 1.5, Number.NaN])("rejects an available package with invalid version code %s", async (versionCode) => {
  const root = await mount(updates({ check: async () => ({ status: "available", versionCode }) }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("업데이트 정보를 확인하지 못했어요."));
  expect(root.querySelector("[data-action='update-apply']")).toBeNull();
});

it("asks before installing the new app and leaves the update available when the sheet is cancelled", async () => {
  let applied = false;
  const root = await mount(updates({ apply: async () => { applied = true; return { status: "started" }; } }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  press(root, "update-apply");
  expect(root.querySelector(".action-sheet")?.textContent).toContain("새 버전의 앱을 설치");
  expect(root.querySelector(".action-sheet")?.textContent).toContain("로그인과 서버의 자리 찾기는 유지");
  expect(root.querySelector(".action-sheet")?.textContent).not.toContain("ZIP");
  press(root, "sheet-cancel");
  expect(applied).toBe(false);
  expect(root.querySelector(".action-sheet")).toBeNull();
  expect(root.querySelector("[data-action='update-apply']")).not.toBeNull();
});

it.each(["cancelled", "started"] as const)("unblocks a fresh check after native installation returns %s", async (status) => {
  let checks = 0;
  const root = await mount(updates({
    check: async () => {
      checks += 1;
      return checks === 1 ? { status: "available", version: "4.23.0", versionCode: 42300 } : { status: "current" };
    },
    apply: async () => ({ status }),
  }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  press(root, "update-apply");
  press(root, "sheet-confirm");
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  expect(root.textContent).toContain(status === "started" ? "Google Play에서 업데이트를 시작했어요" : "업데이트가 취소됐어요");
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("최신 버전이에요"));
  expect(checks).toBe(2);
});

it("tells a direct installer user to finish installation and reopen the app", async () => {
  const root = await mount(updates({
    getStatus: async () => ({ supported: true, source: "apk", currentVersion: "4.22.0", versionCode: 42201 }),
  }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  press(root, "update-apply");
  expect(root.querySelector(".action-sheet")?.textContent).toContain("설치 화면");
  expect(root.querySelector(".action-sheet")?.textContent).toContain("앱을 다시 열어 주세요");
  expect(root.querySelector(".action-sheet")?.textContent).toContain("로그인과 서버의 자리 찾기는 유지");
  press(root, "sheet-confirm");
  await vi.waitFor(() => expect(root.textContent).toContain("설치 화면에서 업데이트를 마친 뒤 앱을 다시 열어 주세요"));
  expect(root.textContent).not.toContain("앱이 다시 열려요");
});

it("reports a failed check and can retry instead of calling it up to date", async () => {
  let online = false;
  const root = await mount(updates({
    check: async () => {
      if (!online) throw new Error("업데이트 서버에 연결하지 못했어요.");
      return { status: "current" };
    },
  }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("업데이트 서버에 연결하지 못했어요."));
  expect(root.textContent).not.toContain("최신 버전이에요");
  online = true;
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("최신 버전이에요"));
});

it("allows a fresh check after the package installer fails", async () => {
  let checks = 0;
  const root = await mount(updates({
    check: async () => {
      checks += 1;
      return { status: "available", version: "4.23.0", versionCode: 42300 };
    },
    apply: async () => { throw new Error("앱 설치를 시작하지 못했어요."); },
  }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  press(root, "update-apply");
  press(root, "sheet-confirm");
  await vi.waitFor(() => expect(root.textContent).toContain("앱 설치를 시작하지 못했어요."));
  const retry = root.querySelector<HTMLButtonElement>("[data-action='update-check']");
  expect(retry?.disabled).toBe(false);
  retry?.click();
  await vi.waitFor(() => expect(checks).toBe(2));
});

it.each([
  [undefined, "이 환경에서는 앱 내 업데이트를 사용할 수 없어요"],
  [updates({ getStatus: async () => ({ supported: false, source: "unsupported", currentVersion: "4.22.0", versionCode: 42201, reason: "공식 배포 앱에서 확인해 주세요." }) }), "공식 배포 앱에서 확인해 주세요."],
] as const)("disables package updates when unavailable without claiming the app is current", async (client, message) => {
  const root = await mount(client);
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(true));
  expect(root.textContent).toContain(message);
  expect(root.textContent).not.toContain("최신 버전이에요");
});

it("replaces a startup error with native guidance when a manual status read succeeds", async () => {
  let reads = 0;
  const root = await mount(updates({
    getStatus: async () => {
      reads += 1;
      if (reads === 1) throw new Error("startup unavailable");
      return { supported: false, source: "unsupported", currentVersion: "4.22.0", versionCode: 42201, reason: "Google Play 또는 배포된 APK에서 확인해 주세요." };
    },
  }));
  await vi.waitFor(() => expect(root.textContent).toContain("업데이트 정보를 읽지 못했어요."));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("Google Play 또는 배포된 APK에서 확인해 주세요."));
  expect(root.textContent).not.toContain("업데이트 정보를 읽지 못했어요.");
  expect(root.textContent).not.toContain("최신 버전이에요");
});

it("does not contact the native updater from a demo", async () => {
  let contacted = false;
  const root = await mount(updates({ getStatus: async () => { contacted = true; throw new Error(); } }), true);
  expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(true);
  expect(contacted).toBe(false);
});

it("ignores a late startup status failure after a newer successful manual check", async () => {
  let rejectStartup!: (error: Error) => void;
  const startup = new Promise<UpdateStatus>((_resolve, reject) => { rejectStartup = reject; });
  let statusReads = 0;
  const root = await mount(updates({
    getStatus: () => ++statusReads === 1
      ? startup
      : Promise.resolve({ supported: true, source: "apk", currentVersion: "4.22.0", versionCode: 42201 }),
  }));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  rejectStartup(new Error("Late status error"));
  await expect(startup).rejects.toThrow("Late status error");
  expect(root.querySelector("[data-action='update-apply']")).not.toBeNull();
  expect(root.textContent).not.toContain("Late status error");
});

it("discards an old check failure after the session restarts and a newer check succeeds", async () => {
  let rejectOld!: (error: Error) => void;
  const old = new Promise<UpdateCheck>((_resolve, reject) => { rejectOld = reject; });
  let checks = 0;
  const root = await mount(updates({
    check: () => ++checks === 1 ? old : Promise.resolve({ status: "available", versionCode: 42300 }),
  }));
  press(root, "update-check");
  const app = apps.at(-1)!;
  await app.start(true);
  app.navigate("settings");
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  rejectOld(new Error("Old session error"));
  await expect(old).rejects.toThrow("Old session error");
  expect(root.querySelector("[data-action='update-apply']")).not.toBeNull();
  expect(root.textContent).not.toContain("Old session error");
});
