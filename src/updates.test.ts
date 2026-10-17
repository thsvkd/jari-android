import { afterEach, expect, it, vi } from "vitest";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";
import type { AppUpdatesClient, UpdateCheck, UpdateStatus } from "./updates";

const apps: JariApp[] = [];
afterEach(() => { apps.splice(0).forEach((app) => app.dispose()); document.body.replaceChildren(); window.localStorage.clear(); });

function updates(overrides: Partial<AppUpdatesClient> = {}): AppUpdatesClient {
  return {
    getStatus: async () => ({ bootId: "boot-1", supported: true, nativeVersion: "4.21.0", currentVersion: "4.21.0" }),
    check: async () => ({ status: "available", version: "4.21.0-web.2" }),
    apply: async () => undefined,
    ready: async () => undefined,
    ...overrides,
  };
}

async function mount(client?: AppUpdatesClient, demoMode = false) {
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, createDemoApi(), { demoMode, updates: client });
  apps.push(app); await app.start(true); app.navigate("settings");
  return root;
}
const press = (root: HTMLElement, action: string) => root.querySelector<HTMLButtonElement>(`[data-action='${action}']`)!.click();

it("checks and applies only after confirmation, and blocks repeat taps during installation", async () => {
  let installing = false;
  let count = 0;
  const root = await mount(updates({ apply: async () => { installing = true; count++; await new Promise(() => undefined); } }));
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-check']")).not.toBeNull());
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("4.21.0-web.2"));
  press(root, "update-apply");
  expect(installing).toBe(false);
  expect(root.querySelector(".action-sheet")?.textContent).toContain("다시 열려요");
  press(root, "sheet-confirm");
  await vi.waitFor(() => expect(installing).toBe(true));
  press(root, "update-apply");
  expect(count).toBe(1);
  expect(root.querySelector<HTMLButtonElement>("[data-action='update-apply']")?.disabled).toBe(true);
});

it("leaves the current screen running when update confirmation is cancelled", async () => {
  let applied = false;
  const root = await mount(updates({ apply: async () => { applied = true; } }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  press(root, "update-apply"); press(root, "sheet-cancel");
  await Promise.resolve();
  expect(applied).toBe(false);
  expect(root.querySelector(".action-sheet")).toBeNull();
  expect(root.querySelector("[data-action='update-apply']")).not.toBeNull();
});

it("reports a failed check and can retry instead of calling it up to date", async () => {
  let online = false;
  const root = await mount(updates({ check: async () => { if (!online) throw new Error("업데이트 서버에 연결하지 못했어요."); return { status: "current" }; } }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("업데이트 서버에 연결하지 못했어요."));
  expect(root.textContent).not.toContain("최신 버전이에요");
  online = true; press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("최신 버전이에요"));
});

it("keeps the running version when applying fails and allows another check", async () => {
  const root = await mount(updates({ apply: async () => { throw new Error("업데이트 파일을 확인하지 못했어요."); } }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  press(root, "update-apply"); press(root, "sheet-confirm");
  await vi.waitFor(() => expect(root.textContent).toContain("업데이트 파일을 확인하지 못했어요."));
  expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false);
  expect(root.querySelector("[data-action='update-apply']")).toBeNull();
});

it("asks for an APK update for an incompatible release", async () => {
  const root = await mount(updates({ check: async () => ({ status: "incompatible", version: "5.0.0" }) }));
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("앱 설치 파일을 업데이트해 주세요"));
  expect(root.querySelector("[data-action='update-apply']")).toBeNull();
});

it.each([undefined, updates({ getStatus: async () => ({ bootId: "boot-1", supported: false, currentVersion: "4.21.0", nativeVersion: "4.21.0" }) })])("disables updating when this installation does not support it", async (client) => {
  const root = await mount(client);
  await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(true));
  expect(root.textContent).toContain("이 환경에서는 앱 내 업데이트를 사용할 수 없어요");
});

it("does not contact the updater from a demo", async () => {
  let contacted = false;
  const root = await mount(updates({ getStatus: async () => { contacted = true; throw new Error(); } }), true);
  expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(true);
  expect(contacted).toBe(false);
});

it("marks the first rendered app shell ready without waiting for the API", async () => {
  let ready = 0;
  const root = document.createElement("div"); document.body.append(root);
  const api = { ...createDemoApi(), bootstrap: () => new Promise<never>(() => undefined) };
  const app = new JariApp(root, api, { demoMode: false, onFirstRender: () => { ready++; } });
  apps.push(app); void app.start(true);
  expect(root.querySelector("main.screen")).not.toBeNull();
  expect(ready).toBe(1);
  app.notify("서버에 연결하고 있어요");
  expect(ready).toBe(1);
});

it("shows a readiness persistence failure instead of silently confirming the update", async () => {
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, createDemoApi(), {
    demoMode: false,
    onFirstRender: async () => { throw new Error("State commit failed"); },
  });
  apps.push(app); await app.start(true);
  await vi.waitFor(() => expect(root.querySelector(".toast")?.textContent).toContain("업데이트 상태를 확정하지 못했어요"));
});

it("keeps readiness failures visible on the sign-in screen", async () => {
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, createDemoApi(), { demoMode: false, onFirstRender: async () => { throw new Error("State commit failed"); } });
  apps.push(app); await app.start(false);
  await vi.waitFor(() => expect(root.querySelector(".update-ready-error")?.textContent).toContain("업데이트 상태를 확정하지 못했어요"));
});

it("ignores a late startup status failure after a newer successful manual check", async () => {
  let rejectStartup!: (error: Error) => void;
  const startup = new Promise<UpdateStatus>((_resolve, reject) => { rejectStartup = reject; });
  let statusReads = 0;
  const root = await mount(updates({ getStatus: () => ++statusReads === 1 ? startup : Promise.resolve({ bootId: "boot-1", supported: true, nativeVersion: "4.21.0", currentVersion: "4.21.0" }) }));
  press(root, "update-check");
  await vi.waitFor(() => expect(root.querySelector("[data-action='update-apply']")).not.toBeNull());
  rejectStartup(new Error("Late status error"));
  await expect(startup).rejects.toThrow("Late status error");
  expect(root.querySelector("[data-action='update-apply']")).not.toBeNull();
});

it("discards an old check failure after the session restarts and a newer check succeeds", async () => {
  let rejectOld!: (error: Error) => void;
  const old = new Promise<UpdateCheck>((_resolve, reject) => { rejectOld = reject; });
  let checks = 0;
  const root = await mount(updates({ check: () => ++checks === 1 ? old : Promise.resolve({ status: "available", version: "web.3" }) }));
  press(root, "update-check");
  const app = apps.at(-1)!;
  await app.start(true); app.navigate("settings");
  expect(root.querySelector<HTMLButtonElement>("[data-action='update-check']")?.disabled).toBe(false);
  press(root, "update-check");
  await vi.waitFor(() => expect(root.textContent).toContain("web.3"));
  rejectOld(new Error("Old session error"));
  await expect(old).rejects.toThrow("Old session error");
  expect(root.querySelector("[data-action='update-apply']")).not.toBeNull();
  expect(root.textContent).not.toContain("Old session error");
});
