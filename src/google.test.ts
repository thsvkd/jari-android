import { afterEach, expect, it, vi } from "vitest";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";
import type { GoogleAuthPoll } from "./types";
import { ApiError } from "./api";

const mounted: JariApp[] = [];
afterEach(() => { mounted.splice(0).forEach((app) => app.dispose()); vi.useRealTimers(); document.body.replaceChildren(); });

it("opens Google externally and saves only the resulting app session", async () => {
  const demo = createDemoApi();
  const auth = await demo.login({ username: "friend", password: "password8" });
  const onToken = vi.fn(async () => undefined);
  const onOpenExternal = vi.fn(async () => undefined);
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleConfiguration: async () => ({ enabled: true }),
    googleStart: async () => ({ ticket: "private-proof", authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=opaque", expiresIn: 300, verificationCode: "ABCD-EFGH" }),
    googlePoll: async () => ({ status: "complete" as const, auth }),
  }, { demoMode: false, onToken, onOpenExternal });
  mounted.push(app);
  await app.start(false);
  root.querySelector<HTMLButtonElement>("[data-action='google-login']")!.click();
  await vi.waitFor(() => expect(onToken).toHaveBeenCalledWith(auth.token));
  expect(onOpenExternal).toHaveBeenCalledWith(expect.stringContaining("https://accounts.google.com/"));
  expect(root.textContent).not.toContain("private-proof");
  await vi.waitFor(() => expect(root.querySelector(".screen-home")).not.toBeNull());
});

it("links with the existing app session and shows the linked status without a password", async () => {
  const demo = createDemoApi();
  let linked = false;
  const googleLink = vi.fn(async () => ({ ticket: "link-proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300, verificationCode: "ABCD-EFGH" }));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleConfiguration: async () => ({ enabled: true }),
    googleStatus: async () => ({ enabled: true, linked }),
    googleLink,
    googlePoll: async () => { linked = true; return { status: "complete" as const, linked: true as const }; },
  }, { demoMode: false, onOpenExternal: async () => undefined });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  root.querySelector<HTMLButtonElement>("[data-action='google-link']")!.click();
  await vi.waitFor(() => expect(googleLink).toHaveBeenCalledWith());
  expect(root.querySelector("#sheet-password")).toBeNull();
  await vi.waitFor(() => expect(root.textContent).toContain("구글 계정 연결됨"));
});

it.each([true, false, undefined])("ignores a legacy reauthentication hint (%s) and links without the local password", (reauthenticationRequired) => runDirectLink(reauthenticationRequired));

async function runDirectLink(reauthenticationRequired: boolean | undefined): Promise<void> {
  const demo = createDemoApi();
  const googleLink = vi.fn(async () => ({ ticket: "recent-proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300, verificationCode: "ABCD-EFGH" }));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleStatus: async () => ({ enabled: true, linked: false, reauthenticationRequired }),
    googleLink, googlePoll: async () => ({ status: "pending" as const }),
  }, { demoMode: false, onOpenExternal: async () => undefined });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  root.querySelector<HTMLButtonElement>("[data-action='google-link']")!.click();
  await vi.waitFor(() => expect(googleLink).toHaveBeenCalledWith());
  expect(root.querySelector("#sheet-password")).toBeNull();
  await vi.waitFor(() => expect(root.querySelector("[data-google-code]")?.textContent).toBe("ABCD-EFGH"));
}

it("shows a link failure without opening a password prompt or silently retrying", async () => {
  const demo = createDemoApi();
  const googleLink = vi.fn().mockRejectedValue(new ApiError("연결을 시작하지 못했어요.", 403, "server", "reauthentication_required"));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleStatus: async () => ({ enabled: true, linked: false, reauthenticationRequired: false }),
    googleLink,
  }, { demoMode: false, onOpenExternal: async () => undefined });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  root.querySelector<HTMLButtonElement>("[data-action='google-link']")!.click();
  await vi.waitFor(() => expect(root.textContent).toContain("연결을 시작하지 못했어요."));
  expect(root.querySelector("#sheet-password")).toBeNull();
  expect(googleLink).toHaveBeenCalledTimes(1);
});

it("still asks for the password before unlinking a recently authenticated account", async () => {
  const demo = createDemoApi();
  const googleUnlink = vi.fn(async () => ({ linked: false as const }));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleStatus: async () => ({ enabled: true, linked: true, reauthenticationRequired: false }), googleUnlink,
  }, { demoMode: false });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  root.querySelector<HTMLButtonElement>("[data-action='google-unlink']")!.click();
  await vi.waitFor(() => expect(root.querySelector("#sheet-password")).not.toBeNull());
  expect(googleUnlink).not.toHaveBeenCalled();
});

it("keeps cancellation retry visible when cancellation fails during an in-flight login", async () => {
  const demo = createDemoApi();
  let complete!: (value: GoogleAuthPoll) => void;
  const onToken = vi.fn(async () => undefined);
  const googleCancel = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ cancelled: true });
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleConfiguration: async () => ({ enabled: true }), googleCancel,
    googleStart: async () => ({ ticket: "proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300, verificationCode: "ABCD-EFGH" }),
    googlePoll: () => new Promise((resolve) => { complete = resolve; }),
  }, { demoMode: false, onToken, onOpenExternal: async () => undefined });
  mounted.push(app);
  await app.start(false);
  root.querySelector<HTMLButtonElement>("[data-action='google-login']")!.click();
  await vi.waitFor(() => expect(complete).toBeTypeOf("function"));
  root.querySelector<HTMLButtonElement>("[data-action='google-cancel']")!.click();
  await vi.waitFor(() => expect(root.textContent).toContain("취소를 확인하지 못했어요"));
  expect(root.querySelector("[data-action='google-cancel']")).not.toBeNull();
  complete({ status: "complete", auth: await demo.login({ username: "friend", password: "password8" }) });
  await Promise.resolve();
  expect(onToken).not.toHaveBeenCalled();
  root.querySelector<HTMLButtonElement>("[data-action='google-cancel']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".google-pending")).toBeNull());
  expect(googleCancel).toHaveBeenNthCalledWith(1, "proof");
  expect(googleCancel).toHaveBeenNthCalledWith(2, "proof");
});
