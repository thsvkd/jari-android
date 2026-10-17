import { afterEach, expect, it, vi } from "vitest";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";
import type { GoogleAuthPoll } from "./types";
import { ApiError } from "./api";

const mounted: JariApp[] = [];
afterEach(() => { mounted.splice(0).forEach((app) => app.dispose()); vi.useRealTimers(); document.body.replaceChildren(); window.localStorage.clear(); });

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

it("unlinks after a confirmation, without asking for the app password", async () => {
  const demo = createDemoApi();
  let linked = true;
  const googleUnlink = vi.fn(async () => { linked = false; return { linked: false as const }; });
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleStatus: async () => ({ enabled: true, linked, reauthenticationRequired: false }), googleUnlink,
  }, { demoMode: false });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  await vi.waitFor(() => expect(root.querySelector("[data-action='google-unlink']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-action='google-unlink']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).not.toBeNull());
  expect(root.querySelector("#sheet-password")).toBeNull();
  expect(root.textContent).toContain("다른 휴대폰의 앱 로그인도 함께 해제돼요.");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-cancel']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).toBeNull());
  expect(googleUnlink).not.toHaveBeenCalled();
  root.querySelector<HTMLButtonElement>("[data-action='google-unlink']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='google-link']")).not.toBeNull());
  expect(googleUnlink).toHaveBeenCalledWith();
  expect(root.textContent).toContain("구글 계정 연결을 해제했어요.");
});

it("signs this phone out when it unlinks from a session that Google signed in", async () => {
  const demo = createDemoApi();
  const googleUnlink = vi.fn(async () => ({ linked: false as const, signedOut: true }));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    googleConfiguration: async () => ({ enabled: true }),
    googleStatus: async () => ({ enabled: true, linked: true, reauthenticationRequired: false, signedInWithGoogle: true }), googleUnlink,
  }, { demoMode: false });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  await vi.waitFor(() => expect(root.querySelector("[data-action='google-unlink']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-action='google-unlink']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).not.toBeNull());
  expect(root.textContent).toContain("이 휴대폰을 포함해 모든 휴대폰에서 로그아웃돼요.");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".auth-shell")).not.toBeNull());
  expect(googleUnlink).toHaveBeenCalledTimes(1);
  expect(root.textContent).toContain("구글 계정 연결을 해제하고 로그아웃했어요.");
  expect(root.querySelector(".screen-settings")).toBeNull();
});

it("reads a 401 that lands before the unlink answer as that sign-out, not as an expired login", async () => {
  const demo = createDemoApi();
  let app!: JariApp;
  // The status poll got the 401 first: the API layer expires the session while the unlink is still out.
  const googleUnlink = vi.fn(async () => { app.expireSession(); return { linked: false as const, signedOut: true }; });
  const root = document.createElement("div"); document.body.append(root);
  app = new JariApp(root, { ...demo,
    googleConfiguration: async () => ({ enabled: true }),
    googleStatus: async () => ({ enabled: true, linked: true, reauthenticationRequired: false, signedInWithGoogle: true }), googleUnlink,
  }, { demoMode: false });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  await vi.waitFor(() => expect(root.querySelector("[data-action='google-unlink']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-action='google-unlink']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".auth-shell")).not.toBeNull());
  expect(root.textContent).toContain("구글 계정 연결을 해제하고 로그아웃했어요.");
  expect(root.textContent).not.toContain("로그인이 만료됐어요");
  // Afterwards an ordinary expiry reads as one again.
  app.expireSession();
  expect(root.textContent).toContain("로그인이 만료됐어요");
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

const HANDOFF = "h".repeat(43);

it("on Android signs in with no code once the browser hands the result back to this app", async () => {
  const demo = createDemoApi();
  const auth = await demo.login({ username: "friend", password: "password8" });
  const onToken = vi.fn(async () => undefined);
  const googleStart = vi.fn(async () => ({ ticket: "private-proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300 }));
  const googlePoll = vi.fn(async (_ticket: string, handoff?: string): Promise<GoogleAuthPoll> => handoff === HANDOFF ? { status: "complete", auth } : { status: "pending" });
  const googleAcknowledge = vi.fn(async () => ({ acknowledged: true }));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo, googleConfiguration: async () => ({ enabled: true }), googleStart, googlePoll, googleAcknowledge },
    { demoMode: false, onToken, onOpenExternal: async () => undefined, googleAppReturn: true });
  mounted.push(app);
  await app.start(false);
  root.querySelector<HTMLButtonElement>("[data-action='google-login']")!.click();
  await vi.waitFor(() => expect(googlePoll).toHaveBeenCalledWith("private-proof", undefined));
  expect(googleStart).toHaveBeenCalledWith({ appReturn: true });
  expect(root.querySelector("[data-google-code]")).toBeNull();
  expect(root.querySelector(".google-pending")?.textContent).toContain("자리났다 앱 열기");
  expect(onToken).not.toHaveBeenCalled();

  app.receiveGoogleHandoff(HANDOFF);
  await vi.waitFor(() => expect(onToken).toHaveBeenCalledWith(auth.token));
  expect(googlePoll).toHaveBeenLastCalledWith("private-proof", HANDOFF);
  expect(googleAcknowledge).toHaveBeenCalledWith("private-proof");
  await vi.waitFor(() => expect(root.querySelector(".screen-home")).not.toBeNull());
  // The browser page may reopen the app with the same value; it is not a new sign-in.
  app.receiveGoogleHandoff(HANDOFF);
  expect(root.textContent).not.toContain("진행 중인 구글 로그인이 없어요");
});

it("goes from a Google sign-in to home without a failure card while the first state loads", async () => {
  const demo = createDemoApi();
  const auth = await demo.login({ username: "friend", password: "password8" });
  let loaded!: () => void;
  const bootstrap = vi.fn(() => new Promise<Awaited<ReturnType<typeof demo.bootstrap>>>((resolve) => { loaded = () => void demo.bootstrap().then(resolve); }));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo, bootstrap, googleConfiguration: async () => ({ enabled: true }),
    googleStart: async () => ({ ticket: "private-proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300 }),
    googlePoll: async (_ticket: string, handoff?: string): Promise<GoogleAuthPoll> => handoff === HANDOFF ? { status: "complete", auth } : { status: "pending" },
    googleAcknowledge: async () => ({ acknowledged: true }),
  }, { demoMode: false, onToken: async () => undefined, onOpenExternal: async () => undefined, googleAppReturn: true });
  mounted.push(app);
  await app.start(false);
  root.querySelector<HTMLButtonElement>("[data-action='google-login']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".google-pending")).not.toBeNull());
  app.receiveGoogleHandoff(HANDOFF);
  await vi.waitFor(() => expect(bootstrap).toHaveBeenCalled());
  expect(root.querySelector(".blocker")).not.toBeNull();
  expect(root.textContent).not.toContain("앱을 열지 못했어요");
  loaded();
  await vi.waitFor(() => expect(root.querySelector(".blocker")).toBeNull());
  expect(root.querySelector(".screen-home")).not.toBeNull();
  expect(root.querySelector(".screen-home")!.textContent).not.toContain("앱을 열지 못했어요");
});

it("sends a handoff that arrives during an in-flight poll right after it", async () => {
  vi.useFakeTimers();
  const demo = createDemoApi();
  let answer!: (value: GoogleAuthPoll) => void;
  const googlePoll = vi.fn((_ticket: string, handoff?: string) => handoff
    ? Promise.resolve<GoogleAuthPoll>({ status: "complete", linked: true })
    : new Promise<GoogleAuthPoll>((resolve) => { answer = resolve; }));
  const googleLink = vi.fn(async () => ({ ticket: "link-proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300 }));
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo, googleStatus: async () => ({ enabled: true, linked: false }), googleLink, googlePoll },
    { demoMode: false, onOpenExternal: async () => undefined, googleAppReturn: true });
  mounted.push(app);
  await app.start(true);
  app.navigate("settings");
  root.querySelector<HTMLButtonElement>("[data-action='google-link']")!.click();
  await vi.waitFor(() => expect(googlePoll).toHaveBeenCalledTimes(1));
  expect(googleLink).toHaveBeenCalledWith({ appReturn: true });
  app.receiveGoogleHandoff(HANDOFF);
  expect(googlePoll).toHaveBeenCalledTimes(1);
  answer({ status: "pending" });
  await vi.advanceTimersByTimeAsync(0);
  expect(googlePoll).toHaveBeenLastCalledWith("link-proof", HANDOFF);
  await vi.waitFor(() => expect(root.textContent).toContain("구글 계정을 연결했어요."));
});

it("refuses a handoff from someone else's sign-in link and says there is nothing to finish", async () => {
  const demo = createDemoApi();
  const onToken = vi.fn(async () => undefined);
  const googleCancel = vi.fn(async () => ({ cancelled: true }));
  const googlePoll = vi.fn(async (_ticket: string, handoff?: string): Promise<GoogleAuthPoll> => {
    if (handoff) throw new ApiError("이 앱에서 시작한 구글 로그인이 아니에요. 다시 시작해 주세요.", 403, "server");
    return { status: "pending" };
  });
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo, googleConfiguration: async () => ({ enabled: true }), googlePoll, googleCancel,
    googleStart: async () => ({ ticket: "own-proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300 }),
  }, { demoMode: false, onToken, onOpenExternal: async () => undefined, googleAppReturn: true });
  mounted.push(app);
  await app.start(false);

  app.receiveGoogleHandoff(HANDOFF);
  expect(root.textContent).toContain("진행 중인 구글 로그인이 없어요. 앱에서 다시 시작해 주세요.");
  expect(googlePoll).not.toHaveBeenCalled();

  // The same spent value again (the page's button, an old tab, a replayed launch) is not sent with a new flow.
  root.querySelector<HTMLButtonElement>("[data-action='google-login']")!.click();
  await vi.waitFor(() => expect(googlePoll).toHaveBeenCalledTimes(1));
  app.receiveGoogleHandoff(HANDOFF);
  await Promise.resolve();
  expect(googlePoll).toHaveBeenCalledTimes(1);
  expect(googlePoll).not.toHaveBeenCalledWith("own-proof", HANDOFF);
  root.querySelector<HTMLButtonElement>("[data-action='google-cancel']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".google-pending")).toBeNull());

  root.querySelector<HTMLButtonElement>("[data-action='google-login']")!.click();
  await vi.waitFor(() => expect(googlePoll).toHaveBeenCalledTimes(2));
  app.receiveGoogleHandoff("o".repeat(43));
  await vi.waitFor(() => expect(root.textContent).toContain("이 앱에서 시작한 구글 로그인이 아니에요."));
  expect(root.querySelector(".google-pending")).toBeNull();
  expect(onToken).not.toHaveBeenCalled();
});

it("keeps the browser code when the server predates the app return", async () => {
  const demo = createDemoApi();
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo, googleConfiguration: async () => ({ enabled: true }), googlePoll: async () => ({ status: "pending" }),
    googleStart: async () => ({ ticket: "old-proof", authorizationUrl: "https://accounts.google.com/auth", expiresIn: 300, verificationCode: "ABCD-EFGH" }),
  }, { demoMode: false, onOpenExternal: async () => undefined, googleAppReturn: true });
  mounted.push(app);
  await app.start(false);
  root.querySelector<HTMLButtonElement>("[data-action='google-login']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-google-code]")?.textContent).toBe("ABCD-EFGH"));
  app.receiveGoogleHandoff(HANDOFF);
  expect(root.querySelector("[data-google-code]")?.textContent).toBe("ABCD-EFGH");
});
