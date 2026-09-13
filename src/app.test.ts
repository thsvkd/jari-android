import { afterEach, describe, expect, it, vi } from "vitest";

import { TeumApp } from "./app";
import { createDemoApi } from "./demo";
import type { StatusResult } from "./types";

const mounted: TeumApp[] = [];

afterEach(() => {
  for (const app of mounted.splice(0)) app.dispose();
  vi.useRealTimers();
  document.body.replaceChildren();
});

describe("concept C application shell", () => {
  it("exposes the active destination in the portrait bottom navigation", async () => {
    const { app, root } = await mountLive();

    expect(root.querySelector(".home-layout")).not.toBeNull();
    expect(root.querySelector(".screen-home")).not.toBeNull();
    expect(root.querySelector("[data-view='home'][aria-current='page']")).not.toBeNull();

    app.navigate("activity");

    expect(root.querySelector(".screen-activity")).not.toBeNull();
    expect(root.querySelector("[data-view='activity'][aria-current='page']")).not.toBeNull();
    expect(root.querySelector("[data-view='home'][aria-current='page']")).toBeNull();
  });

  it("uses accessible icon controls for notifications and back navigation", async () => {
    const { root } = await mountLive();
    const notificationButton = root.querySelector<HTMLButtonElement>("[aria-label='알림 열기']");

    expect(notificationButton?.querySelector("svg")).not.toBeNull();
    expect(root.querySelector(".brand-mark")?.textContent).toBe("");

    notificationButton?.click();
    const backButton = root.querySelector<HTMLButtonElement>("[aria-label='뒤로가기']");
    expect(backButton?.querySelector("svg")).not.toBeNull();

    backButton?.click();
    expect(root.querySelector(".screen-home")).not.toBeNull();
  });

  it.each(["running", "scheduled"])("prioritizes payment without borrowing the %s journey", async (other) => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    state.running!.srcLocate = "광명";
    if (other === "scheduled") {
      state.scheduled = { startAt: null, timeZone: "Asia/Seoul", search: state.running! };
      state.running = null;
    }
    state.pending = [{ reservationId: "TEST", trainInfo: "KTX 015 서울 → 부산", expiresAt: null, seatNumber: null }];
    const { root } = await mountLive({ bootstrap: async () => state });
    const card = root.querySelector("[data-search-status]");
    expect(card?.textContent).toContain("빈자리를 찾았어요");
    expect(card?.textContent).toContain("예약 확인하기");
    expect(card?.textContent).not.toContain("광명");
  });

  it("groups route, readable seat conditions and details navigation in the chosen status card", async () => {
    const { root } = await mountLive();
    const card = root.querySelector<HTMLElement>("[data-search-status]");
    expect(card?.textContent).toContain("서울");
    expect(card?.textContent).toContain("부산");
    expect(card?.textContent).toContain("일반실 우선");
    expect(card?.textContent).toContain("1명");
    card?.querySelector<HTMLButtonElement>("[data-view='activity']")?.click();
    expect(root.querySelector("[data-action='cancel-search']")).not.toBeNull();
  });

  it.each(["unavailable", "stale", "idle", "scheduled", "pending", "offline"] as const)("keeps %s distinguishable in the status card", async (scenario) => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    if (scenario === "unavailable" || scenario === "stale") state.running!.health = scenario;
    if (scenario === "scheduled") state.scheduled = { startAt: "2026-09-19T00:00:00Z", timeZone: "Asia/Seoul", search: state.running! };
    if (scenario === "pending") state.pending = [{ reservationId: "TEST", trainInfo: "KTX 015 서울 → 부산", expiresAt: null, seatNumber: null }];
    if (["idle", "scheduled", "pending"].includes(scenario)) state.running = null;
    if (scenario === "offline") vi.useFakeTimers();
    const { root } = await mountLive({ bootstrap: async () => state, ...(scenario === "offline" ? { status: async () => { throw new Error("network unavailable"); } } : {}) });
    if (scenario === "offline") await vi.advanceTimersByTimeAsync(30_000);
    const wanted = { unavailable: "철도 조회를 완료하지 못했어요", stale: "한동안 조회 결과가 없어요", idle: "진행 중인 검색이 없어요", scheduled: "예약한 시각에 검색을 시작해요", pending: "빈자리를 찾았어요", offline: "현재 상태를 확인할 수 없어요" };
    expect(root.querySelector("[data-search-status]")?.textContent).toContain(wanted[scenario]);
    if (scenario !== "idle") expect(root.querySelector("[data-search-status]")?.textContent).not.toContain("진행 중인 검색이 없어요");
  });

  it("keeps an unverified running search visible in the static status card", async () => {
    const demoApi = createDemoApi(() => new Date("2026-09-12T00:00:00Z"));
    const state = await demoApi.bootstrap();
    state.demo = false;
    state.running = {
      depDate: "20260919",
      srcLocate: "서울",
      dstLocate: "부산",
      depTime: "0730",
      maxDepTime: "1200",
      trainTypeShow: "KTX 계열만",
      specialInfoShow: "GENERAL_FIRST",
      passengerCount: 1,
      seatStrategy: "consecutive",
      seatPreference: "",
      selectedTrains: ["015"],
      startedAt: "2026-09-12T00:00:00Z",
    };
    const api = { ...demoApi, bootstrap: async () => state };
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);

    expect(root.querySelector<HTMLElement>("[data-search-status]")?.dataset.state).toBe("running-unverified");
    expect(root.textContent).toContain("검색은 서버에 등록돼 있어요");
    expect(root.textContent).not.toContain("방금 확인");
  });

  it("explains the integrated SRT routes and disables unsupported waitlist and scheduling", async () => {
    const demoApi = createDemoApi();
    const state = await demoApi.bootstrap();
    state.demo = false;
    state.capabilities = {
      ...state.capabilities,
      srt: false,
      waitlist: false,
      scheduledSearch: false,
    };
    const api = { ...demoApi, bootstrap: async () => state };
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);
    app.navigate("journey");

    expect(root.querySelector("[data-operator='srt']")).toBeNull();
    expect(root.querySelector<HTMLInputElement>("[name='waitlist']")?.disabled).toBe(true);
    expect(root.textContent).toContain("기존 SRT 노선은 KTX로 통합됐어요");
    expect(root.textContent).toContain("코레일 계정 하나로 검색하고 예약할 수 있어요");

    app.navigate("confirm");
    expect(root.querySelector<HTMLButtonElement>("[data-action='schedule-toggle']")?.disabled).toBe(
      true,
    );
  });

  it("marks membership controls as admin-only and hides them from members", async () => {
    const demoApi = createDemoApi();
    const adminState = await demoApi.bootstrap();
    adminState.user = { id: "admin", username: "operator", role: "admin" };
    const adminRoot = document.createElement("div");
    document.body.append(adminRoot);
    const adminApp = new TeumApp(adminRoot, { ...demoApi, bootstrap: async () => adminState }, { demoMode: false });
    mounted.push(adminApp);
    await adminApp.start(true);
    adminApp.navigate("settings");

    expect(adminRoot.querySelector(".admin-only-badge")?.textContent).toBe("관리자 전용");
    expect(adminRoot.textContent).toContain("회원 가입 권한은 관리자만 발급할 수 있어요");
    expect(adminRoot.querySelector("[data-action='create-invite']")).not.toBeNull();
    expect(adminRoot.textContent).toContain("v4.8.0");
    expect(adminRoot.textContent).not.toContain("베타");

    const memberState = await demoApi.bootstrap();
    memberState.user = { id: "member", username: "traveller", role: "member" };
    const memberRoot = document.createElement("div");
    document.body.append(memberRoot);
    const memberApp = new TeumApp(memberRoot, { ...demoApi, bootstrap: async () => memberState }, { demoMode: false });
    mounted.push(memberApp);
    await memberApp.start(true);
    memberApp.navigate("settings");

    expect(memberRoot.querySelector(".admin-only-badge")).toBeNull();
    expect(memberRoot.querySelector("[data-action='create-invite']")).toBeNull();
  });

  it("marks the fixture-only experience on every demo screen", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, createDemoApi(), { demoMode: true });
    mounted.push(app);

    await app.start(true);

    expect(root.querySelector("[data-demo-banner]")?.textContent).toContain("데모 모드");
    expect(root.textContent).toContain("실제 조회·예약 없음");
  });

  it("prevents unsupported booking, favourites, and notification settings", async () => {
    const demoApi = createDemoApi();
    const state = await demoApi.bootstrap();
    state.demo = false;
    state.capabilities = {
      ...state.capabilities,
      korail: false,
      favourites: false,
      notificationSettings: false,
    };
    const api = { ...demoApi, bootstrap: async () => state };
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);
    app.navigate("journey");
    expect(root.querySelector<HTMLButtonElement>("#conditions-form button[type='submit']")?.disabled).toBe(true);
    app.navigate("favourites");
    expect(root.textContent).toContain("즐겨찾기를 이용할 수 없어요");
    app.navigate("settings");
    expect(root.querySelector<HTMLButtonElement>("[data-action='notify-plus']")?.disabled).toBe(true);
  });

  it("offers the real access-request action when the server requires approval", async () => {
    const demoApi = createDemoApi();
    const requestAccess = vi.fn().mockResolvedValue({ requested: true });
    const api = {
      ...demoApi,
      search: vi.fn().mockResolvedValue({ started: false, needsAccessRequest: true }),
      requestAccess,
    };
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, api, { demoMode: true });
    mounted.push(app);

    await app.start(true);
    app.navigate("confirm");
    root.querySelector<HTMLButtonElement>("[data-action='start-now']")?.click();
    await vi.waitFor(() =>
      expect(root.querySelector<HTMLButtonElement>("[data-action='request-access']")).not.toBeNull(),
    );
    root.querySelector<HTMLButtonElement>("[data-action='request-access']")?.click();
    await vi.waitFor(() => expect(requestAccess).toHaveBeenCalledOnce());
  });

  it("keeps edited journey fields when passenger controls rerender the form", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, createDemoApi(), { demoMode: true });
    mounted.push(app);
    await app.start(true);
    app.navigate("journey");
    const source = root.querySelector<HTMLInputElement>("[name='src_station']")!;
    source.value = "용산";
    source.dispatchEvent(new Event("change", { bubbles: true }));

    root.querySelector<HTMLButtonElement>("[data-action='passenger-plus']")?.click();

    expect(root.querySelector<HTMLInputElement>("[name='src_station']")?.value).toBe("용산");
    expect(root.textContent).toContain("2명");
  });

  it("never collects railway credentials in demo mode", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, createDemoApi(), { demoMode: true });
    mounted.push(app);
    await app.start(true);

    app.navigate("rail-account");

    expect(root.querySelector("input[type='password']")).toBeNull();
    expect(root.textContent).toContain("코레일 아이디와 비밀번호를 받지 않고");
  });

  it("does not trust telemetry fields when the server capability is off", async () => {
    const demoApi = createDemoApi(() => new Date("2026-09-12T00:00:00Z"));
    const state = await demoApi.bootstrap();
    state.demo = false;
    state.capabilities.lastChecked = false;
    const api = { ...demoApi, bootstrap: async () => state };
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);

    expect(root.querySelector<HTMLElement>("[data-search-status]")?.dataset.state).toBe("running-unverified");
    expect(root.textContent).toContain("최근 조회 상태는 알 수 없어요");
  });

  it("matches the server app-account credential bounds", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const app = new TeumApp(root, createDemoApi(), { demoMode: false });
    mounted.push(app);
    await app.start(false);
    root.querySelector<HTMLButtonElement>("[data-auth-gate='guest']")?.click();

    const username = root.querySelector<HTMLInputElement>("[name='username']")!;
    const password = root.querySelector<HTMLInputElement>("[name='password']")!;
    expect(username.maxLength).toBe(32);
    expect(username.pattern).toBe("[A-Za-z0-9_]{3,32}");
    expect(password.minLength).toBe(12);
    expect(password.maxLength).toBe(128);
    root.querySelector<HTMLButtonElement>("[data-auth-mode='register']")?.click();
    const invite = root.querySelector<HTMLInputElement>("[name='invite']")!;
    expect(invite.minLength).toBe(16);
    expect(invite.maxLength).toBe(128);
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function mountLive(overrides: Partial<ReturnType<typeof createDemoApi>> = {}) {
  const api = { ...createDemoApi(), ...overrides };
  const root = document.createElement("div");
  document.body.append(root);
  const app = new TeumApp(root, api, { demoMode: false });
  mounted.push(app);
  await app.start(true);
  return { app, root, api };
}

function edit(root: HTMLElement, name: string, value: string) {
  const input = root.querySelector<HTMLInputElement>(`[name='${name}']`)!;
  if (input.type === "radio") {
    root.querySelector<HTMLInputElement>(`[name='${name}'][value='${value}']`)!.click();
    return;
  }
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("account isolation regressions", () => {
  it("start(false) erases notifications, booking draft, status and selected trains before another account", async () => {
    const { app, root } = await mountLive();
    app.navigate("notifications");
    await vi.waitFor(() => expect(root.querySelector(".notification")).not.toBeNull());
    app.navigate("journey");
    edit(root, "src_station", "광명");
    app.notify("private account A message");
    await app.start(false);
    expect(app).toMatchObject({ state: null, notifications: [], conditions: null, selectedTrains: [], trainOptions: [], history: [], toast: "", busy: false, connection: "unknown" });
    expect(root.textContent).not.toContain("private account A message");
    await app.start(true);
    expect(app).toMatchObject({ notifications: [] });
  });

  it("ignores a deferred notification response after switching accounts", async () => {
    const pending = deferred<Awaited<ReturnType<ReturnType<typeof createDemoApi>["notifications"]>>>();
    const { app, root } = await mountLive({ notifications: () => pending.promise });
    app.navigate("notifications");
    await app.start(false);
    await app.start(true);
    pending.resolve({ items: [{ id: "old", text: "private A notification", kind: "reservation", createdAt: "2026-09-12T00:00:00Z" }], pushAvailable: true });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(app).toMatchObject({ notifications: [] });
    expect(root.textContent).not.toContain("private A notification");
  });

  it("ignores a deferred bootstrap from the previous account", async () => {
    const state = await createDemoApi().bootstrap();
    const pending = deferred<typeof state>();
    const root = document.createElement("div");
    document.body.append(root);
    const api = { ...createDemoApi(), bootstrap: vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValue({ ...state, draft: null }) };
    const app = new TeumApp(root, api, { demoMode: false });
    mounted.push(app);
    const first = app.start(true);
    await app.start(false);
    await app.start(true);
    pending.resolve(state);
    await first;
    expect(app).toMatchObject({ conditions: null, state: { draft: null } });
  });
});

describe("editable form rerender regressions", () => {
  it("preserves Gwangmyeong and 13:00 through passenger, end-of-day, toast and back navigation DOM events", async () => {
    const { app, root } = await mountLive();
    app.navigate("journey");
    edit(root, "src_station", "광명");
    edit(root, "dep_time", "13:00");
    root.querySelector<HTMLButtonElement>("[data-action='passenger-plus']")!.click();
    root.querySelector<HTMLInputElement>("[name='unlimited_time']")!.click();
    app.notify("background update");
    expect(root.querySelector<HTMLInputElement>("[name='src_station']")!.value).toBe("광명");
    expect(root.querySelector<HTMLInputElement>("[name='dep_time']")!.value).toBe("13:00");
    app.navigate("home");
    app.back();
    expect(root.querySelector<HTMLInputElement>("[name='src_station']")!.value).toBe("광명");
    expect(root.querySelector<HTMLInputElement>("[name='dep_time']")!.value).toBe("13:00");
  });

  it("preserves uncommitted journey edits during background rerender", async () => {
    const { app, root } = await mountLive();
    app.navigate("journey");
    edit(root, "src_station", "광명");
    edit(root, "dep_time", "13:00");
    app.notify("background update");
    expect(root.querySelector<HTMLInputElement>("[name='src_station']")!.value).toBe("광명");
    expect(root.querySelector<HTMLInputElement>("[name='dep_time']")!.value).toBe("13:00");
  });

  it("preserves favourite name and scheduled time during toast rerender", async () => {
    const { app, root } = await mountLive();
    app.navigate("confirm");
    root.querySelector<HTMLButtonElement>("[data-action='schedule-toggle']")!.click();
    root.querySelector<HTMLInputElement>("#favourite-name")!.value = "private route";
    root.querySelector<HTMLInputElement>("#schedule-at")!.value = "2026-09-20T13:00";
    app.notify("background update");
    expect(root.querySelector<HTMLInputElement>("#favourite-name")!.value).toBe("private route");
    expect(root.querySelector<HTMLInputElement>("#schedule-at")!.value).toBe("2026-09-20T13:00");
  });
});

it.each(["success", "auth", "offline"] as const)("ignores deferred A polling %s after account B starts", async (outcome) => {
  const { ApiError } = await import("./api");
  const pending = deferred<Awaited<ReturnType<ReturnType<typeof createDemoApi>["status"]>>>();
  const status = vi.fn().mockReturnValue(pending.promise);
  const { app, root } = await mountLive({ status });
  vi.useFakeTimers();
  // Restart to establish polling on the fake clock.
  await app.start(true);
  await vi.advanceTimersByTimeAsync(30_000);
  expect(status).toHaveBeenCalledOnce();
  await app.start(false);
  await app.start(true);
  const before = root.innerHTML;
  if (outcome === "success") pending.resolve({ running: null, scheduled: null, pending: [{ trainInfo: "PRIVATE_A" }] } as StatusResult);
  else pending.reject(new ApiError("PRIVATE_A", outcome === "auth" ? 401 : 0, outcome));
  await vi.advanceTimersByTimeAsync(0);
  expect(root.innerHTML).toBe(before);
  expect(root.textContent).not.toContain("PRIVATE_A");
  app.dispose();
  vi.useRealTimers();
});

it.each(["logout", "auth expiry", "direct switch"])("clears all personal UI state on %s", async (transition) => {
  const { ApiError } = await import("./api");
  const { app, root, api } = await mountLive();
  app.navigate("notifications");
  await vi.waitFor(() => expect(root.querySelector(".notification")).not.toBeNull());
  app.navigate("journey");
  edit(root, "src_station", "광명");
  app.navigate("confirm");
  root.querySelector<HTMLInputElement>("#favourite-name")!.value = "PRIVATE_A";
  app.notify("PRIVATE_A");
  if (transition === "logout") {
    app.navigate("settings");
    root.querySelector<HTMLButtonElement>("[data-action='app-logout']")!.click();
  } else if (transition === "auth expiry") {
    api.search = async () => { throw new ApiError("expired", 401, "auth"); };
    root.querySelector<HTMLButtonElement>("[data-action='start-now']")!.click();
    await vi.waitFor(() => expect(root.querySelector("[data-auth-gate='admin']")).not.toBeNull());
  } else {
    await app.start(true);
  }
  expect(app).toMatchObject({ notifications: [], trainOptions: [], selectedTrains: [], toast: "", scheduleOpen: false, accessRequired: false });
  expect(root.textContent).not.toContain("PRIVATE_A");
  if (transition !== "direct switch") {
    expect(app).toMatchObject({ state: null, conditions: null, history: [], busy: false });
    expect(root.querySelector("[data-auth-gate='admin']")).not.toBeNull();
  }
});

it("preserves every journey field through background polling, swap and toast timeout", async () => {
  const { app, root } = await mountLive();
  vi.useFakeTimers();
  await app.start(true);
  app.navigate("journey");
  root.querySelector<HTMLButtonElement>("[data-action='passenger-plus']")!.click();
  const values = { src_station: "광명", dst_station: "대전", dep_date: "2026-09-22", dep_time: "13:45", max_dep_time: "18:30", seat_row_min: "4", seat_row_max: "8", train_type: "2", seat_option: "4", seat_strategy: "2" };
  for (const [name, value] of Object.entries(values)) edit(root, name, value);
  app.notify("background");
  await vi.advanceTimersByTimeAsync(30_000);
  for (const [name, value] of Object.entries(values)) {
    const input = root.querySelector<HTMLInputElement>(`[name='${name}']`)!;
    expect(input.type === "radio" ? root.querySelector<HTMLInputElement>(`[name='${name}']:checked`)!.value : input.value).toBe(value);
  }
  root.querySelector<HTMLButtonElement>("[data-action='swap']")!.click();
  expect(root.querySelector<HTMLInputElement>("[name='src_station']")!.value).toBe("대전");
  expect(root.querySelector<HTMLInputElement>("[name='dst_station']")!.value).toBe("광명");
  app.dispose();
  vi.useRealTimers();
});

it("keeps the edited end time when end-of-day is toggled on and off", async () => {
  const { app, root } = await mountLive();
  app.navigate("journey");
  edit(root, "max_dep_time", "18:30");
  root.querySelector<HTMLInputElement>("[name='unlimited_time']")!.click();
  app.notify("background");
  root.querySelector<HTMLInputElement>("[name='unlimited_time']")!.click();
  expect(root.querySelector<HTMLInputElement>("[name='max_dep_time']")!.value).toBe("18:30");
});

it("discards a deferred login token after the auth generation is reset", async () => {
  const pending = deferred<Awaited<ReturnType<ReturnType<typeof createDemoApi>["login"]>>>();
  const onToken = vi.fn();
  const root = document.createElement("div");
  document.body.append(root);
  const app = new TeumApp(root, { ...createDemoApi(), login: () => pending.promise }, { demoMode: false, onToken });
  mounted.push(app);
  await app.start(false);
  root.querySelector<HTMLButtonElement>("[data-auth-gate='guest']")!.click();
  edit(root, "username", "private_A");
  edit(root, "password", "private_A_password");
  root.querySelector("#auth-form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await app.start(false);
  pending.resolve({ token: "A", user: { id: "A", username: "private_A" }, expiresAt: "future" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  expect(onToken).not.toHaveBeenCalled();
  expect(root.querySelector("#auth-form")).toBeNull();
  expect(root.querySelector("[data-auth-gate='guest']")).not.toBeNull();
});

it("keeps the seat arrangement when passenger controls temporarily hide it", async () => {
  const { app, root } = await mountLive();
  app.navigate("journey");
  root.querySelector<HTMLButtonElement>("[data-action='passenger-plus']")!.click();
  root.querySelector<HTMLInputElement>("[name='seat_strategy'][value='2']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='passenger-minus']")!.click();
  app.notify("background");
  root.querySelector<HTMLButtonElement>("[data-action='passenger-plus']")!.click();
  expect(root.querySelector<HTMLInputElement>("[name='seat_strategy']:checked")!.value).toBe("2");
});
