import { readFile } from "node:fs/promises";

import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import { JariApp } from "./app";
import { ApiError } from "./api";
import { createDemoApi } from "./demo";
import appPackage from "../package.json";
import type { SeatInventory, SeatMapSeat, SeatTarget, StatusResult } from "./types";

const mounted: JariApp[] = [];

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
    // The payment card is the status now; a second "found a seat" card only repeated it.
    expect(root.querySelector("[data-search-status]")).toBeNull();
    const payment = root.querySelector(".payment-card")?.textContent;
    expect(payment).toContain("KTX 015 서울 → 부산");
    expect(payment).not.toContain("광명");
  });

  it("groups route, window and details navigation in the chosen status card", async () => {
    const { root } = await mountLive();
    const card = root.querySelector<HTMLElement>("[data-search-status]");
    expect(card?.textContent).toContain("서울");
    expect(card?.textContent).toContain("부산");
    expect(card?.textContent).toContain("1명");
    card?.querySelector<HTMLButtonElement>("[data-view='activity']")?.click();
    expect(root.querySelector("[data-action='cancel-search']")).not.toBeNull();
  });

  it.each(["unavailable", "stale", "idle", "scheduled", "offline"] as const)("keeps %s distinguishable in the status card", async (scenario) => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    if (scenario === "unavailable" || scenario === "stale") state.running!.health = scenario;
    if (scenario === "scheduled") state.scheduled = { startAt: "2026-09-19T00:00:00Z", timeZone: "Asia/Seoul", search: state.running! };
    if (["idle", "scheduled"].includes(scenario)) state.running = null;
    if (scenario === "offline") vi.useFakeTimers();
    const { root } = await mountLive({ bootstrap: async () => state, ...(scenario === "offline" ? { status: async () => { throw new Error("network unavailable"); } } : {}) });
    // A miss is confirmed by a second poll five seconds later before the card calls it offline.
    if (scenario === "offline") await vi.advanceTimersByTimeAsync(35_000);
    const wanted = { unavailable: "철도 조회를 완료하지 못했어요", stale: "한동안 조회 결과가 없어요", idle: "대기 중인 항목이 없어요", scheduled: "정한 시각에 찾기 시작해요", offline: "현재 상태를 확인할 수 없어요" };
    expect(root.querySelector("[data-search-status]")?.textContent).toContain(wanted[scenario]);
    if (scenario !== "idle") expect(root.querySelector("[data-search-status]")?.textContent).not.toContain("대기 중인 항목이 없어요");
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
    const app = new JariApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);

    expect(root.querySelector<HTMLElement>("[data-search-status]")?.dataset.state).toBe("running-unverified");
    expect(root.querySelector("[data-search-status]")?.textContent).toContain("찾는 중");
    expect(root.textContent).not.toContain("검색은 서버에 등록돼 있어요");
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
    const app = new JariApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);
    app.navigate("journey");

    expect(root.querySelector("[data-operator='srt']")).toBeNull();
    expect(root.querySelector<HTMLInputElement>("[name='waitlist']")).toBeNull();
    expect(root.textContent).toContain("기존 SRT 노선은 KTX로 통합됐어요");
    expect(root.textContent).toContain("코레일 계정 하나로 조회하고 예약할 수 있어요");

    app.navigate("confirm");
    expect(root.querySelector<HTMLButtonElement>("[data-action='schedule-toggle']")?.disabled).toBe(
      true,
    );
  });

  it("submits an eligible single train as a Korail waitlist request", async () => {
    const demoApi = createDemoApi();
    const state = await demoApi.bootstrap();
    state.demo = false;
    state.running = null;
    state.capabilities.waitlist = true;
    const search = vi.fn().mockResolvedValue({ started: false, waitlisted: true, trainNo: "015" });
    const { app, root } = await mountLive({ bootstrap: async () => state, search });

    app.navigate("journey");
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-official-waitlist='015']")).not.toBeNull());
    expect(root.querySelector("[data-official-waitlist='019']")).toBeNull();
    root.querySelector<HTMLButtonElement>("[data-official-waitlist='015']")!.click();
    expect(root.querySelector(".action-sheet")?.textContent).toContain("코레일 예약 대기를 신청할까요?");
    root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
    await vi.waitFor(() => expect(search).toHaveBeenCalledOnce());
    expect(search.mock.calls[0]![0]).toMatchObject({
      conditions: { waitlist: true },
      trains: ["015"],
    });
    await vi.waitFor(() => expect(root.querySelector(".screen-home")).not.toBeNull());
  });

  it("marks membership controls as admin-only and hides them from members", async () => {
    const demoApi = createDemoApi();
    const adminState = await demoApi.bootstrap();
    adminState.user = { id: "admin", username: "operator", role: "admin" };
    const adminRoot = document.createElement("div");
    document.body.append(adminRoot);
    const adminApp = new JariApp(adminRoot, { ...demoApi, bootstrap: async () => adminState }, { demoMode: false });
    mounted.push(adminApp);
    await adminApp.start(true);
    adminApp.navigate("settings");

    expect(adminRoot.querySelector(".admin-only-badge")?.textContent).toBe("관리자 전용");
    expect(adminRoot.textContent).toContain("회원 가입 권한은 관리자만 발급할 수 있어요");
    expect(adminRoot.querySelector("[data-action='create-invite']")).not.toBeNull();
    expect(adminRoot.textContent).toContain(`v${appPackage.version}`);
    expect(adminRoot.textContent).not.toContain("베타");

    const memberState = await demoApi.bootstrap();
    memberState.user = { id: "member", username: "traveller", role: "member" };
    const memberRoot = document.createElement("div");
    document.body.append(memberRoot);
    const memberApp = new JariApp(memberRoot, { ...demoApi, bootstrap: async () => memberState }, { demoMode: false });
    mounted.push(memberApp);
    await memberApp.start(true);
    memberApp.navigate("settings");

    expect(memberRoot.querySelector(".admin-only-badge")).toBeNull();
    expect(memberRoot.querySelector("[data-action='create-invite']")).toBeNull();
  });

  it("marks the fixture-only experience on every demo screen", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const app = new JariApp(root, createDemoApi(), { demoMode: true });
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
    const app = new JariApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);
    app.navigate("journey");
    expect(root.querySelector<HTMLButtonElement>("#conditions-form button[type='submit']")?.disabled).toBe(true);
    app.navigate("favourites");
    expect(root.textContent).toContain("즐겨찾기를 이용할 수 없어요");
    app.navigate("settings");
    expect(root.querySelector<HTMLButtonElement>("[data-action='notify-plus']")?.disabled).toBe(true);
  });

  it("loads a favourite into the journey form from 조건 수정, without a stale train selection", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-14T03:00:00+09:00"));
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    state.favourites[0]!.conditions = {
      ...state.favourites[0]!.conditions,
      dep_date: "",
      trains: ["015"],
    };
    const { app, root } = await mountLive({ bootstrap: async () => state });

    app.navigate("favourites");
    root.querySelector<HTMLButtonElement>("[data-edit-favourite='demo-home']")!.click();

    expect(root.querySelector<HTMLInputElement>("[name='dep_date']")!.value).toBe("2026-09-14");
    expect(root.querySelector<HTMLInputElement>("[name='src_station']")!.value).toBe("서울");
    expect(root.querySelector<HTMLInputElement>("[name='dst_station']")!.value).toBe("부산");
    expect(root.querySelector("[data-train='015']")).toBeNull();
  });

  it("starts a new journey with today's time window instead of the previous server draft", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-14T10:37:00+09:00"));
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    state.draft = {
      ...state.draft!,
      dep_date: "20260915",
      dep_time: "0700",
      max_dep_time: "1200",
      passenger_count: 2,
    };
    const { root } = await mountLive({ bootstrap: async () => state });

    root.querySelector<HTMLButtonElement>("[data-action='new-journey']")!.click();

    expect(root.querySelector<HTMLInputElement>("[name='dep_date']")!.value).toBe("2026-09-14");
    expect(root.querySelector<HTMLInputElement>("[name='dep_time']")!.value).toBe("10:40");
    expect(root.querySelector<HTMLInputElement>("[name='max_dep_time']")!.value).toBe("12:40");
    expect(root.querySelector(".stepper output")?.textContent).toBe("1명");
  });

  it("restores a cancellation-wait seat plan carried in the bootstrap draft", async () => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    const targets: SeatTarget[] = [{
      carNo: 3, seatNo: "demo-1-A", label: "1A", row: 1, column: "A",
      direction: "1", floor: "", adjacencyGroup: "1:left", position: 1, rowPosition: 1,
    }];
    state.draft = {
      ...state.draft!,
      trains: ["015"],
      seat_plan: { strategy: "independent", passengerCount: 1, trains: [{ trainNo: "015", seatClass: "general", targets }] },
    };
    const { app, root } = await mountLive({ bootstrap: async () => state });

    expect(app).toMatchObject({ cancellationTargets: [{ trainNo: "015", seatClass: "general", targets }] });

    app.navigate("journey");
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
    expect(root.querySelector("[data-train-no='015'][data-seat-class='general'] em")?.textContent).toBe("선택 완료 · 1석");
    expect(root.querySelector("[data-action='trains-next']")!.textContent).toContain("1편 선택");
  });

  it("keeps the special-class button for restored special seats when the cabin does not matter", async () => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    const targets: SeatTarget[] = [{
      carNo: 1, seatNo: "demo-1-A", label: "1A", row: 1, column: "A",
      direction: "1", floor: "", adjacencyGroup: "1:left", position: 1, rowPosition: 1,
    }];
    state.draft = {
      ...state.draft!,
      trains: ["015"],
      seat_plan: { strategy: "independent", passengerCount: 1, trains: [{ trainNo: "015", seatClass: "special", targets }] },
    };
    const { app, root } = await mountLive({ bootstrap: async () => state });

    app.navigate("journey");
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='special']")).not.toBeNull());
    expect(root.querySelector("[data-train-no='015'][data-seat-class='special'] em")?.textContent).toBe("선택 완료 · 1석");
    expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")?.textContent).toContain("좌석 지정");
  });

  it("drops a restored cancellation-wait target once a re-search no longer lists its train", async () => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    const targets: SeatTarget[] = [{
      carNo: 3, seatNo: "demo-1-A", label: "1A", row: 1, column: "A",
      direction: "1", floor: "", adjacencyGroup: "1:left", position: 1, rowPosition: 1,
    }];
    state.draft = {
      ...state.draft!,
      trains: ["015"],
      seat_plan: { strategy: "independent", passengerCount: 1, trains: [{ trainNo: "015", seatClass: "general", targets }] },
    };
    const trains = vi.fn()
      .mockResolvedValueOnce({
        trains: [{ no: "015", label: "07:27→10:12 KTX", soldout: true, waitlistEligible: true, generalAvailable: false, specialAvailable: false }],
        truncated: false, passengerCount: 1,
      })
      .mockResolvedValueOnce({
        trains: [{ no: "019", label: "08:03→10:48 KTX", soldout: true, waitlistEligible: false, generalAvailable: false, specialAvailable: false }],
        truncated: false, passengerCount: 1,
      });
    const { app, root } = await mountLive({ bootstrap: async () => state, trains });

    // The re-search still lists train 015, so the restored target and start button survive.
    app.navigate("journey");
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-train-no='015']")).not.toBeNull());
    expect(root.querySelector("[data-action='trains-next']")!.textContent).toContain("1편 선택");

    // A different route/date drops 015 from the results, so the stale target (and its start button) must go too.
    app.navigate("journey");
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-train-no='019']")).not.toBeNull());
    expect(root.querySelector("[data-train-no='015']")).toBeNull();
    expect(root.querySelector("[data-action='trains-next']")!.textContent).not.toContain("편 선택");
  });

  it("fills in v/action when a restored server draft predates those fields", async () => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    const targets: SeatTarget[] = [{
      carNo: 3, seatNo: "demo-1-A", label: "1A", row: 1, column: "A",
      direction: "1", floor: "", adjacencyGroup: "1:left", position: 1, rowPosition: 1,
    }];
    const { v: _v, action: _action, ...draftWithoutAppFields } = state.draft!;
    state.draft = {
      ...draftWithoutAppFields,
      trains: ["015"],
      seat_plan: { strategy: "independent", passengerCount: 1, trains: [{ trainNo: "015", seatClass: "general", targets }] },
    } as typeof state.draft;
    const search = vi.fn().mockResolvedValue({ started: true, running: null });
    const { app, root } = await mountLive({ bootstrap: async () => state, search });

    app.navigate("journey");
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-action='trains-next']")!.textContent).toContain("1편 선택"));
    root.querySelector<HTMLButtonElement>("[data-action='trains-next']")!.click();
    root.querySelector<HTMLButtonElement>("[data-action='start-now']")!.click();

    await vi.waitFor(() => expect(search).toHaveBeenCalledOnce());
    expect(search.mock.calls[0]![0].conditions).toMatchObject({ v: 1, action: "prepare_search" });
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
    const app = new JariApp(root, api, { demoMode: true });
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
    const app = new JariApp(root, createDemoApi(), { demoMode: true });
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
    const app = new JariApp(root, createDemoApi(), { demoMode: true });
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
    const app = new JariApp(root, api, { demoMode: false });
    mounted.push(app);

    await app.start(true);

    expect(root.querySelector<HTMLElement>("[data-search-status]")?.dataset.state).toBe("running-unverified");
    expect(root.textContent).not.toContain("최근 조회 상태는 알 수 없어요");
  });

  it("matches the server app-account credential bounds", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const app = new JariApp(root, createDemoApi(), { demoMode: false });
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
  const app = new JariApp(root, api, { demoMode: false });
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
    const app = new JariApp(root, api, { demoMode: false });
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

  it("picks journey times with the themed picker and keeps them through a rerender", async () => {
    // 밤 10시가 넘으면 기본값이 "마지막 열차까지"라 끝 시각이 꺼져요. 낮으로 고정해요.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 23, 10));
    const { app, root } = await mountLive();
    app.navigate("journey");
    const picker = (id: string) => root.querySelector<HTMLElement>(`[data-time-picker='${id}']`)!;
    picker("max-dep-time").querySelector<HTMLButtonElement>("[data-tp='toggle']")!.click();
    picker("max-dep-time").querySelector<HTMLButtonElement>("[data-tp-hour='18']")!.click();
    picker("max-dep-time").querySelector<HTMLButtonElement>("[data-tp-minute='30']")!.click();
    picker("dep-time").querySelector<HTMLButtonElement>("[data-tp='toggle']")!.click();
    picker("dep-time").querySelector<HTMLButtonElement>("[data-tp-hour='13']")!.click();
    app.notify("background update");
    expect(root.querySelector<HTMLInputElement>("[name='max_dep_time']")!.value).toBe("18:30");
    expect(root.querySelector<HTMLInputElement>("[name='dep_time']")!.value.slice(0, 3)).toBe("13:");
    // 시만 고른 칸은 펼친 채로 다시 그려져요.
    expect(picker("dep-time").querySelector(".time-panel")).not.toBeNull();
    root.querySelector<HTMLInputElement>("[name='unlimited_time']")!.click();
    expect(picker("max-dep-time").querySelector<HTMLButtonElement>("[data-tp='toggle']")!.disabled).toBe(true);
    vi.useRealTimers();
  });

  it("combines the scheduled date and time into #schedule-at, empty until a time is picked", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 23, 10));
    const { app, root } = await mountLive();
    app.navigate("confirm");
    root.querySelector<HTMLButtonElement>("[data-action='schedule-toggle']")!.click();
    const scheduleAt = () => root.querySelector<HTMLInputElement>("#schedule-at")!.value;
    expect(root.querySelector<HTMLInputElement>("#schedule-date")!.value).toBe("2026-09-23");
    expect(scheduleAt()).toBe("");
    root.querySelector<HTMLButtonElement>("[data-date-picker='schedule-date'] [data-dp='toggle']")!.click();
    root.querySelector<HTMLButtonElement>("[data-dp-day='2026-09-25']")!.click();
    root.querySelector<HTMLButtonElement>("[data-time-picker='schedule-time'] [data-tp='toggle']")!.click();
    root.querySelector<HTMLButtonElement>("[data-time-picker='schedule-time'] [data-tp-hour='0']")!.click();
    root.querySelector<HTMLButtonElement>("[data-time-picker='schedule-time'] [data-tp-minute='20']")!.click();
    expect(scheduleAt()).toBe("2026-09-25T00:20");
    app.notify("background update");
    expect(scheduleAt()).toBe("2026-09-25T00:20");
    expect(root.querySelector("[data-time-picker='schedule-time'] .time-field")?.textContent).toContain("오전 12:20");
    vi.useRealTimers();
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
  const values = { src_station: "광명", dst_station: "대전", dep_date: "2026-09-22", dep_time: "13:45", max_dep_time: "18:30", train_type: "2", seat_grade_mode: "specific", seat_strategy: "2" };
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
  const app = new JariApp(root, { ...createDemoApi(), login: () => pending.promise }, { demoMode: false, onToken });
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

it("loads the live seat map and submits an exact designated seat", async () => {
  const demo = createDemoApi();
  const reserveDesignated = vi.fn(demo.reserveDesignated);
  const { app, root } = await mountLive({
    seatCars: demo.seatCars,
    seatInventory: demo.seatInventory,
    reserveDesignated,
    status: demo.status,
  });
  app.navigate("journey");
  edit(root, "seat_grade_mode", "specific");
  root.querySelector<HTMLInputElement>("[name='seat_class'][value='general']")!.click();
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='025'][data-seat-mode='immediate']")).not.toBeNull());

  root.querySelector<HTMLButtonElement>("[data-train-no='025'][data-seat-mode='immediate']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-no='demo-1-A']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-seat-no='demo-1-A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!.click();
  // 실제 예약이라 확인 시트에서 한 번 더 눌러요.
  await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).not.toBeNull());
  expect(root.querySelector(".action-sheet")?.textContent).toContain("3호차 1A");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();

  await vi.waitFor(() => expect(reserveDesignated).toHaveBeenCalledOnce());
  expect(reserveDesignated.mock.calls[0]![0]).toMatchObject({
    trainKey: "demo-025",
    seatClass: "general",
    passengerCount: 1,
    carNo: 3,
    seats: [{ label: "1A" }],
  });
});

it("starts cancellation waiting with the selected physical-seat range", async () => {
  const demo = createDemoApi();
  const search = vi.fn().mockResolvedValue({ started: true, running: null });
  const { app, root } = await mountLive({
    seatCars: demo.seatCars,
    seatInventory: demo.seatInventory,
    search,
  });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());

  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-filter='pair:window']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(4);
  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(0);
  for (const action of ["pair:window", "family", "trim:+"]) {
    root.querySelector<HTMLButtonElement>(`[data-seat-filter='${action}']`)!.click();
  }
  expect(root.querySelector("[data-seat-filter='pair:window']")!.getAttribute("aria-pressed")).toBe("true");
  root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='trains-next']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='start-now']")!.click();

  await vi.waitFor(() => expect(search).toHaveBeenCalledOnce());
  expect(search.mock.calls[0]![0]).toMatchObject({
    trains: ["015"],
    conditions: {
      waitlist: false,
      seat_plan: {
        strategy: "independent",
        passengerCount: 1,
        trains: [{ trainNo: "015", seatClass: "general" }],
      },
    },
  });
  const sentTrain = search.mock.calls[0]![0].conditions.seat_plan.trains[0];
  const targets = sentTrain.targets;
  expect(targets.map((seat: { label: string }) => seat.label)).toEqual(["3A", "3D"]);
  // Only the fields SeatTarget.from_payload reads; salePossible/familyLabel would just bloat a big plan.
  expect(Object.keys(targets[0]).sort()).toEqual(
    ["adjacencyGroup", "carNo", "column", "direction", "floor", "label", "position", "row", "rowPosition", "seatNo"].sort(),
  );
  // trainKey is never read by the server; keeping it in the plan just bloats the payload.
  expect(Object.keys(sentTrain).sort()).toEqual(["seatClass", "targets", "trainNo"].sort());
});

function makeCarInventory(carNo: number): SeatInventory {
  const seat = (row: number, column: "A" | "B"): SeatMapSeat => ({
    carNo, seatNo: `${carNo}-${row}-${column}`, label: `${row}${column}`,
    salePossible: true, direction: "1", floor: "", row, column,
    adjacencyGroup: `${carNo}:${row}`, position: column === "A" ? 1 : 2, rowPosition: column === "A" ? 1 : 2, familyLabel: "",
  });
  return {
    carNo, layoutType: 2, arrangementCode: "4", remainingCount: 4, totalCount: 4,
    seats: [seat(1, "A"), seat(1, "B"), seat(2, "A"), seat(2, "B")],
  };
}

const threeCarSeatCars = async () => ({
  cars: [3, 4, 5].map((carNo) => ({ carNo, roomClassName: "일반실", remainingSeatCount: 4, attributes: [] })),
});

const seatInventoryFake = async (_trainKey: string, carNo: number) => makeCarInventory(carNo);

async function openThreeCarWaitDialog(root: HTMLElement, app: JariApp) {
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='apply-all-cars']")).not.toBeNull());
}

it("applies the current filter to every other car with a single batch request", async () => {
  const seatInventory = vi.fn(async (_trainKey: string, carNo: number) => makeCarInventory(carNo));
  const seatInventories = vi.fn(async () => ({
    inventories: [3, 4, 5].map((carNo) => makeCarInventory(carNo)),
    failedCars: [] as number[],
    layoutReference: false,
  }));
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory, seatInventories });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

  await vi.waitFor(() => expect(root.querySelector("[data-seat-car='4'] em")?.textContent).toBe("2"));
  expect(root.querySelector("[data-seat-car='5'] em")?.textContent).toBe("2");
  expect(seatInventories).toHaveBeenCalledTimes(1); // one request for every other car, not one per car
  expect(seatInventory).toHaveBeenCalledTimes(1); // only the initial car 3 load

  // Switching to a car the bulk apply already cached reuses it instead of refetching.
  root.querySelector<HTMLButtonElement>("[data-seat-car='4']")!.click();
  await vi.waitFor(() => expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(2));
  expect(seatInventory).toHaveBeenCalledTimes(1);
});

it("re-picks every car when the condition changes after a bulk apply", async () => {
  const seatInventories = vi.fn(async () => ({
    inventories: [3, 4, 5].map((carNo) => makeCarInventory(carNo)),
    failedCars: [] as number[],
    layoutReference: false,
  }));
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: seatInventoryFake, seatInventories });
  await openThreeCarWaitDialog(root, app);
  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='confirm-seat-dialog']")!.textContent).toBe("6석 중 빈자리 나면 예약"));

  // Widening the condition grows every car, not only the one on screen - and without another request.
  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:B']")!.click();
  expect(root.querySelector("[data-action='confirm-seat-dialog']")!.textContent).toBe("12석 중 빈자리 나면 예약");
  expect(root.querySelector("[data-seat-car='5'] em")?.textContent).toBe("4");
  // Leaving out a car at each end drops its seats at once.
  root.querySelector<HTMLButtonElement>("[data-seat-filter='cars:+']")!.click();
  expect(root.querySelector("[data-action='confirm-seat-dialog']")!.textContent).toBe("4석 중 빈자리 나면 예약");
  expect(seatInventories).toHaveBeenCalledTimes(1);

  // The condition now belongs to every car, so moving to another car keeps its chips on.
  root.querySelector<HTMLButtonElement>("[data-seat-car='4']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-car='4']")!.classList.contains("selected")).toBe(true));
  expect(root.querySelector("[data-seat-filter='col:A']")!.getAttribute("aria-pressed")).toBe("true");
  expect(root.querySelector("[data-seat-filter='col:B']")!.getAttribute("aria-pressed")).toBe("true");
});

it("takes a train as a whole when its card is pressed, and searches it without a seat plan", async () => {
  const search = vi.fn().mockResolvedValue({ started: true, running: null });
  const { app, root } = await mountLive({ search });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-toggle='015']")).not.toBeNull());

  root.querySelector<HTMLButtonElement>("[data-train-toggle='015']")!.click();
  expect(root.querySelector("[data-train-toggle='015']")!.closest(".train-card")!.classList.contains("selected")).toBe(true);
  expect(root.querySelector("[data-train-toggle='015']")!.getAttribute("aria-pressed")).toBe("true");
  expect(root.querySelector("[data-action='trains-next']")!.textContent).toContain("1편 선택");
  // Pressing again lets it go.
  root.querySelector<HTMLButtonElement>("[data-train-toggle='015']")!.click();
  expect(root.querySelector("[data-action='trains-next']")!.textContent).not.toContain("편 선택");
  root.querySelector<HTMLButtonElement>("[data-train-toggle='015']")!.click();

  root.querySelector<HTMLButtonElement>("[data-action='trains-next']")!.click();
  expect(root.textContent).toContain("1편 선택");
  root.querySelector<HTMLButtonElement>("[data-action='start-now']")!.click();
  await vi.waitFor(() => expect(search).toHaveBeenCalledOnce());
  expect(search.mock.calls[0]![0]).toMatchObject({ trains: ["015"] });
  expect(search.mock.calls[0]![0].conditions.seat_plan).toBeUndefined();
});

it("matches by seat label across cars when no filter is set", async () => {
  const seatInventories = vi.fn(async () => ({
    inventories: [3, 4, 5].map((carNo) => makeCarInventory(carNo)),
    failedCars: [] as number[],
    layoutReference: false,
  }));
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: seatInventoryFake, seatInventories });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-seat-no='3-1-A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

  await vi.waitFor(() => expect(root.querySelector("[data-seat-car='4'] em")?.textContent).toBe("1"));
  expect(root.querySelector("[data-seat-car='5'] em")?.textContent).toBe("1");
});

it("does nothing but hint when neither a filter nor a selection exists", async () => {
  const seatInventories = vi.fn();
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: seatInventoryFake, seatInventories });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

  expect(root.textContent).toContain("먼저 이 호차에서 좌석이나 조건을 골라 주세요.");
  expect(seatInventories).not.toHaveBeenCalled();
});

it("keeps the cars that loaded and names the ones the batch response reports as failed", async () => {
  const seatInventories = vi.fn(async () => ({
    inventories: [makeCarInventory(3), makeCarInventory(5)],
    failedCars: [4],
    layoutReference: false,
  }));
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: seatInventoryFake, seatInventories });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

  await vi.waitFor(() => expect(root.querySelector("[data-seat-car='5'] em")?.textContent).toBe("2"));
  expect(root.querySelector("[data-seat-car='4'] em")).toBeNull();
  expect(root.textContent).toContain("4호차 좌석표를 불러오지 못해 빼고 적용했어요.");
});

it("treats a car missing from the batch response (and not named in failedCars either) as failed too", async () => {
  const seatInventories = vi.fn(async () => ({
    inventories: [makeCarInventory(3), makeCarInventory(5)], // car 4 silently absent, not even in failedCars
    failedCars: [] as number[],
    layoutReference: false,
  }));
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: seatInventoryFake, seatInventories });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

  await vi.waitFor(() => expect(root.querySelector("[data-seat-car='5'] em")?.textContent).toBe("2"));
  expect(root.querySelector("[data-seat-car='4'] em")).toBeNull();
  expect(root.textContent).toContain("4호차 좌석표를 불러오지 못해 빼고 적용했어요.");
});

it("keeps the selection untouched and shows the server message when the batch request itself fails (e.g. 429)", async () => {
  const seatInventories = vi.fn().mockRejectedValue(new ApiError("요청이 너무 많아요. 잠시 후 다시 시도해 주세요.", 429));
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: seatInventoryFake, seatInventories });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(2);
  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

  await vi.waitFor(() => expect(root.textContent).toContain("요청이 너무 많아요. 잠시 후 다시 시도해 주세요."));
  expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(2); // car 3's own selection is unchanged
  expect(root.querySelector("[data-seat-car='4'] em")).toBeNull();
  expect(root.querySelector("[data-seat-car='5'] em")).toBeNull();
});

it("shows numeric seat labels and hides synthetic column chips for a 무궁화 car", async () => {
  const numericSeat = (column: "A" | "B", label: string): SeatMapSeat => ({
    carNo: 6, seatNo: `demo-${label}`, label, salePossible: true, direction: "1", floor: "",
    row: 1, column, adjacencyGroup: `1:${column}`, position: column === "A" ? 1 : 2, rowPosition: column === "A" ? 1 : 2, familyLabel: "",
  });
  const numericInventory: SeatInventory = {
    carNo: 6, layoutType: 2, arrangementCode: "4", remainingCount: 2, totalCount: 2,
    seats: [numericSeat("A", "23"), numericSeat("B", "24")],
  };
  const { app, root } = await mountLive({
    seatCars: async () => ({ cars: [{ carNo: 6, roomClassName: "일반실", remainingSeatCount: 2, attributes: [] }] }),
    seatInventory: async () => numericInventory,
  });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();

  await vi.waitFor(() => expect(root.querySelector("[data-seat-no='demo-23']")).not.toBeNull());
  expect(root.querySelector("[data-seat-no='demo-23'] b")?.textContent).toBe("23");
  expect(root.querySelector("[data-seat-no='demo-24'] b")?.textContent).toBe("24");
  expect(root.querySelector("[data-seat-filter='col:A']")).toBeNull();
  expect(root.querySelector("[data-seat-filter='pair:window']")).not.toBeNull();
});

it("labels a nearby-date formation used for a sold-out train", async () => {
  const demo = createDemoApi();
  const { app, root } = await mountLive({
    seatCars: async (...args) => ({
      ...(await demo.seatCars(...args)),
      layoutReference: true,
    }),
    seatInventory: async (...args) => ({
      ...(await demo.seatInventory(...args)),
      layoutReference: true,
    }),
  });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());

  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();

  // The reference layout is only known once the cars load.
  await vi.waitFor(() => expect(root.querySelector("[data-seat-car]")?.textContent).toContain("좌석표"));
  expect(root.querySelector("[role='dialog']")?.textContent).not.toContain("같은 편성");
  expect(root.querySelector("[role='dialog']")?.textContent).not.toContain("현재 예약 가능");
});

it("refreshes the seat map when a chosen seat loses a reservation race", async () => {
  const demo = createDemoApi();
  const seatInventory = vi.fn(demo.seatInventory);
  const reserveDesignated = vi.fn().mockRejectedValue(
    new ApiError("seat changed", 409, "server"),
  );
  const { app, root } = await mountLive({
    seatCars: demo.seatCars,
    seatInventory,
    reserveDesignated,
  });
  app.navigate("journey");
  edit(root, "seat_grade_mode", "specific");
  root.querySelector<HTMLInputElement>("[name='seat_class'][value='general']")!.click();
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='025'][data-seat-mode='immediate']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='025'][data-seat-mode='immediate']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-no='demo-1-A']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-seat-no='demo-1-A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!.click();
  // 실제 예약이라 확인 시트에서 한 번 더 눌러요.
  await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();

  await vi.waitFor(() => expect(seatInventory).toHaveBeenCalledTimes(2));
  expect(root.querySelector("[role='dialog']")).not.toBeNull();
  expect(root.textContent).toContain("선택한 좌석이 방금 판매됐어요");
  expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(0);
});

it("updates the notification interval without covering the screen", async () => {
  const pending = deferred<{ notifyMinutes: number }>();
  const setNotify = vi.fn(() => pending.promise);
  const { app, root } = await mountLive({ setNotify });
  app.navigate("settings");

  root.querySelector<HTMLButtonElement>("[data-action='notify-plus']")!.click();

  expect(root.querySelector(".blocker")).toBeNull();
  expect(root.querySelector(".stepper output")?.textContent).toBe("10분");
  pending.resolve({ notifyMinutes: 10 });
  await vi.waitFor(() => expect(setNotify).toHaveBeenCalledWith(10));
});

it("spins only the invite button while a code is created", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.user!.role = "admin";
  const pending = deferred<{ invite: string; ttlHours: number; expiresAt: string }>();
  const { app, root } = await mountLive({ bootstrap: async () => state, createInvite: () => pending.promise });
  app.navigate("settings");

  root.querySelector<HTMLButtonElement>("[data-action='create-invite']")!.click();

  expect(root.querySelector(".blocker")).toBeNull();
  expect(root.querySelector("[data-action='create-invite'] .inline-spinner")).not.toBeNull();
  pending.resolve({ invite: "INVITE-CODE-123456", ttlHours: 72, expiresAt: "2026-09-17T00:00:00Z" });
  await vi.waitFor(() => expect(root.querySelector(".invite-card")?.textContent).toContain("INVITE-CODE-123456"));
});

it("opens the seat dialog immediately and keeps load failures inside it", async () => {
  const failure = deferred<{ cars: never[] }>();
  const { app, root } = await mountLive({ seatCars: () => failure.promise });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());

  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();
  expect(root.querySelector("[role='dialog'] .seat-loading")).not.toBeNull();
  expect(root.querySelector(".blocker")).toBeNull();

  failure.reject(new Error("layout unavailable"));
  await vi.waitFor(() => expect(root.querySelector("[role='dialog'] .seat-dialog-error")?.textContent).toContain("좌석표를 불러오지 못했어요"));
  expect(root.querySelector("[data-action='retry-seat-dialog']")).not.toBeNull();
});

it("preserves the seat-map scroll position when seats are selected", async () => {
  const demo = createDemoApi();
  const { app, root } = await mountLive({ seatCars: demo.seatCars, seatInventory: demo.seatInventory });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-no='demo-1-A']")).not.toBeNull());

  const before = root.querySelector<HTMLElement>(".seat-map-live")!;
  before.scrollTop = 96;
  root.querySelector<HTMLButtonElement>("[data-seat-no='demo-1-A']")!.click();

  expect(root.querySelector<HTMLElement>(".seat-map-live")!.scrollTop).toBe(96);
});

it("shows approved empty-state copy and illustrated SVG marks", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.favourites = [];
  state.running = null;
  const { app, root } = await mountLive({ bootstrap: async () => state });

  app.navigate("favourites");
  expect(root.textContent).toContain("즐겨찾기가 없어요");
  expect(root.textContent).toContain("자주 가는 구간을 저장해 두면 다음 찾기가 더 빨라져요.");
  expect(root.textContent).toContain("새 즐겨찾기 추가");
  expect(root.querySelector(".empty-mark svg")).not.toBeNull();

  app.navigate("activity");
  expect(root.querySelector(".empty-mark svg")).not.toBeNull();
});

it("reports a failed notification request instead of an empty inbox", async () => {
  const { app, root } = await mountLive({ notifications: async () => { throw new ApiError("알림 서버 오류", 500); } });

  app.navigate("notifications");
  await vi.waitFor(() => expect(root.textContent).toContain("알림을 불러오지 못했어요"));

  expect(root.querySelector("[role='alert']")!.textContent).toBe("알림 서버 오류");
  expect(root.textContent).not.toContain("새 알림이 없어요");
});

it("shows a server error during polling as unconfirmed, not as offline", async () => {
  const status = vi.fn().mockRejectedValue(new ApiError("잠시 후 다시 시도해 주세요.", 503));
  const { app, root } = await mountLive({ status });
  vi.useFakeTimers();
  // Restart to establish polling on the fake clock.
  await app.start(true);
  await vi.advanceTimersByTimeAsync(30_000);

  expect(root.querySelector(".offline-banner")).toBeNull();
  expect(root.textContent).toContain("현재 상태를 확인할 수 없어요");
  expect(root.textContent).not.toContain("인터넷 연결이 복구되면");
});

it("shows the offline banner only after a failed poll is confirmed five seconds later", async () => {
  const status = vi.fn().mockRejectedValue(new ApiError("서버에 연결하지 못했어요.", 0, "offline"));
  const { app, root } = await mountLive({ status });
  vi.useFakeTimers();
  await app.start(true);
  await vi.advanceTimersByTimeAsync(30_000);
  // One miss: no banner yet, a second look is pending.
  expect(root.querySelector(".offline-banner")).toBeNull();
  await vi.advanceTimersByTimeAsync(5_000);
  expect(root.querySelector(".offline-banner")).not.toBeNull();

  status.mockResolvedValue({ running: null, scheduled: null, pending: [] });
  await vi.advanceTimersByTimeAsync(30_000);
  expect(root.querySelector(".offline-banner")).toBeNull();
});

it.each([[1, 10], [-1, 5]])("moves an off-step notify interval by %i to the neighbouring step", async (direction, expected) => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.notifyMinutes = 7;
  const setNotify = vi.fn().mockImplementation(async (minutes: number) => ({ notifyMinutes: minutes }));
  const { app, root } = await mountLive({ bootstrap: async () => state, setNotify });

  app.navigate("settings");
  root.querySelector<HTMLButtonElement>(`[data-action='${direction > 0 ? "notify-plus" : "notify-minus"}']`)!.click();

  await vi.waitFor(() => expect(setNotify).toHaveBeenCalledWith(expected));
});

it("reaches the confirm screen from the train list by clicks alone and saves a favourite there", async () => {
  const demo = createDemoApi();
  const saveFavourite = vi.fn(demo.saveFavourite);
  const { root } = await mountLive({ saveFavourite });

  root.querySelector<HTMLButtonElement>("[data-action='new-journey']")!.click();
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-action='trains-next']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-action='trains-next']")!.click();

  expect(root.querySelector("[data-action='start-now']")).not.toBeNull();
  root.querySelector<HTMLButtonElement>("[data-action='save-favourite']")!.click();
  await vi.waitFor(() => expect(saveFavourite).toHaveBeenCalledOnce());
});

it("keeps the running home card to a badge, the route and its two shortcuts", async () => {
  const cancelSearch = vi.fn(async () => ({ stopped: true, unscheduled: false }));
  const { root } = await mountLive({ cancelSearch });
  const card = root.querySelector<HTMLElement>("[data-search-status]")!;

  expect(card.dataset.state).toBe("healthy");
  expect(card.textContent).toContain("찾는 중");
  expect(card.textContent).toContain("1명");
  expect(card.textContent).toContain("자세히 보기");
  expect(card.textContent).toContain("그만 찾기");
  expect(card.textContent).not.toContain("빈자리를 찾고 있어요");
  expect(card.textContent).not.toContain("앱을 닫아도 서버에서 계속 검색해요.");
  expect(card.textContent).not.toContain("마지막 조회");
  expect(card.querySelector(".search-last-check, .search-status-description")).toBeNull();

  card.querySelector<HTMLButtonElement>("[data-action='stop-search']")!.click();
  expect(root.querySelector(".action-sheet")?.textContent).toContain("자리 찾기를 그만할까요?");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(cancelSearch).toHaveBeenCalledOnce());
});

it("keeps the route chips under the heading in every state, and drops the shelf when there is none", async () => {
  const { root } = await mountLive();
  const shelf = root.querySelector<HTMLElement>(".chip-shelf")!;

  expect(shelf.textContent).toContain("최근 구간 바로가기");
  expect([...shelf.querySelectorAll<HTMLButtonElement>(".idle-chip")].map((chip) => chip.dataset.routeChip)).toEqual(["recent", "demo-home"]);
  expect(shelf.compareDocumentPosition(root.querySelector("[data-search-status]")!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.draft = null;
  state.favourites = [];
  const bare = await mountLive({ bootstrap: async () => state });
  expect(bare.root.querySelector(".chip-shelf")).toBeNull();
  expect(bare.root.querySelector("[data-search-status]")?.textContent).toContain("찾는 중");
});

it("prints the check time once in 검색 상세, as a relative time next to the count", async () => {
  const { app, root } = await mountLive();
  app.navigate("activity");
  const card = root.querySelector<HTMLElement>(".activity-card")!;

  expect(card.querySelector(".notice")).toBeNull();
  expect(card.querySelector(".activity-check")?.textContent).toBe("방금 확인 · 18회 조회");
  // 헤더는 시각을 되풀이하지 않아요.
  expect(card.querySelector(".row-between small")?.textContent).toBe("");
  // 시작 시각은 별도 카드가 아니라 조건 표의 한 행이에요.
  expect(root.querySelector(".status-guide-card")).toBeNull();
  expect(card.querySelector(".activity-conditions")?.textContent).toContain("시작");
});

it("keeps the notice in 검색 상세 when the search is in trouble", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running!.health = "error";
  const { app, root } = await mountLive({ bootstrap: async () => state });
  app.navigate("activity");

  expect(root.querySelector(".activity-card .notice.warning")?.textContent).toContain("철도 조회를 완료하지 못했어요");
});

async function mountIdle(overrides: Partial<ReturnType<typeof createDemoApi>> = {}) {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running = null;
  return mountLive({ bootstrap: async () => state, ...overrides });
}

it("offers the restored search and the favourites as dated shortcuts above the quiet idle card", async () => {
  const { root } = await mountIdle();
  const shelf = root.querySelector<HTMLElement>(".chip-shelf")!;
  const chips = [...shelf.querySelectorAll<HTMLButtonElement>(".idle-chip")];

  expect(chips.map((chip) => chip.dataset.routeChip)).toEqual(["recent", "demo-home"]);
  expect(chips[0]!.textContent).toContain("최근 · 07:00–12:00");
  expect(chips[1]!.textContent).toContain("주말에 집으로 · 14:00–18:00");
  const card = root.querySelector("[data-search-status]")!;
  expect(shelf.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(card.textContent?.trim()).toBe("대기 중인 항목이 없어요");
  expect(card.querySelector(".empty-mark")).not.toBeNull();
  expect(card.querySelector(".idle-badge")).toBeNull();
  // The card never repeats the full-width button below it.
  expect(root.querySelectorAll("[data-action='new-journey']")).toHaveLength(1);
});

it("shows only the quiet card when there is no route to shortcut", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running = null;
  state.draft = null;
  state.favourites = [];
  const { root } = await mountLive({ bootstrap: async () => state });

  expect(root.querySelector(".chip-shelf")).toBeNull();
  expect(root.querySelector("[data-search-status]")?.textContent?.trim()).toBe("대기 중인 항목이 없어요");
});

it.each([
  ["1100", "2026-09-14"],
  ["1000", "2026-09-15"],
  ["0900", "2026-09-15"],
])("preselects the chip's date as today, or tomorrow once its window has (nearly) passed (%s)", async (maxDepTime, expected) => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-14T09:30:00+09:00"));
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running = null;
  state.draft = { ...state.draft!, max_dep_time: maxDepTime };
  const { root } = await mountLive({ bootstrap: async () => state });

  root.querySelector<HTMLButtonElement>("[data-route-chip='recent']")!.click();

  expect(root.querySelector(".action-sheet")?.textContent).toContain("언제 떠나세요?");
  expect(root.querySelector<HTMLInputElement>("#sheet-date")!.value).toBe(expected);
});

it("searches the chip's trip on the chosen date and keeps the trains that still run", async () => {
  const trains = vi.fn(createDemoApi().trains);
  const { root } = await mountIdle({ trains });

  root.querySelector<HTMLButtonElement>("[data-route-chip='demo-home']")!.click();
  expect(root.querySelector(".action-sheet")?.textContent).toContain("인원 1명 · 14:00–18:00 · 고른 열차 3편");
  root.querySelector<HTMLInputElement>("#sheet-date")!.value = "2026-10-03";
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();

  await vi.waitFor(() => expect(root.querySelector(".train-list")).not.toBeNull());
  expect(trains.mock.calls[0]![0].conditions).toMatchObject({
    dep_date: "20261003",
    src_station: "서울",
    dst_station: "부산",
    dep_time: "1400",
    max_dep_time: "1800",
    passenger_count: 1,
    train_type: "1",
    seat_plan: undefined,
  });
  // 015 and 019 are listed again, 099 is not; the button counts only the two that survived.
  expect(root.querySelector("[data-action='trains-next']")?.textContent).toContain("2편 선택");
  expect([...root.querySelectorAll(".train-card.selected [data-train-toggle]")].map((train) => (train as HTMLElement).dataset.trainToggle)).toEqual(["015", "019"]);
  expect(root.querySelector(".toast")?.textContent).toBe("3편 중 1편은 이 날 운행하지 않아 뺐어요.");
});

it("keeps the date sheet dismissable without starting a search", async () => {
  const trains = vi.fn(createDemoApi().trains);
  const { root } = await mountIdle({ trains });

  root.querySelector<HTMLButtonElement>("[data-route-chip='recent']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='sheet-cancel']")!.click();

  expect(root.querySelector(".action-sheet")).toBeNull();
  expect(trains).not.toHaveBeenCalled();
  expect(root.querySelector(".screen-home")).not.toBeNull();
});

it.each(["home", "favourites"] as const)("opens the date sheet from a favourite row on %s and searches that date", async (view) => {
  const trains = vi.fn(createDemoApi().trains);
  const { app, root } = await mountLive({ trains });
  if (view === "favourites") app.navigate("favourites");

  root.querySelector<HTMLButtonElement>("[data-use-favourite='demo-home']")!.click();
  expect(root.querySelector(".action-sheet")?.textContent).toContain("언제 떠나세요?");
  root.querySelector<HTMLInputElement>("#sheet-date")!.value = "2026-10-03";
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();

  await vi.waitFor(() => expect(root.querySelector(".train-list")).not.toBeNull());
  expect(trains.mock.calls[0]![0].conditions).toMatchObject({ dep_date: "20261003", dep_time: "1400", max_dep_time: "1800" });
  // 즐겨찾기를 눌러 여정 입력 폼으로 돌아가는 길은 더 이상 없어요.
  expect(root.querySelector("#conditions-form")).toBeNull();
});

it("asks in an in-app sheet instead of window.confirm, and only acts when it is confirmed", async () => {
  const cancelSearch = vi.fn(async () => ({ stopped: true, unscheduled: false }));
  const { app, root } = await mountLive({ cancelSearch });
  app.navigate("activity");

  root.querySelector<HTMLButtonElement>("[data-action='cancel-search']")!.click();
  const sheet = root.querySelector<HTMLElement>(".action-sheet");
  expect(sheet?.getAttribute("role")).toBe("dialog");
  expect(sheet?.getAttribute("aria-modal")).toBe("true");
  expect(sheet?.textContent).toContain("자리 찾기를 그만할까요?");

  root.querySelector<HTMLButtonElement>("[data-action='sheet-cancel']")!.click();
  expect(root.querySelector(".action-sheet")).toBeNull();
  expect(cancelSearch).not.toHaveBeenCalled();

  root.querySelector<HTMLButtonElement>("[data-action='cancel-search']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(cancelSearch).toHaveBeenCalledOnce());
});

it("keeps solid red for the last confirmation only; the screen's own stop button is outlined", async () => {
  const { app, root } = await mountLive();
  app.navigate("activity");

  expect(root.querySelector("[data-action='cancel-search']")?.className).toBe("button ghost danger");
  root.querySelector<HTMLButtonElement>("[data-action='cancel-search']")!.click();
  expect(root.querySelector("[data-action='sheet-confirm']")?.className).toBe("button danger");
});

it("asks nothing through the browser's own dialog any more", async () => {
  const source = await readFile("src/app.ts", "utf8");
  expect(source).not.toContain("window.confirm");
});

it.each([
  ["backdrop", (root: HTMLElement) => root.querySelector<HTMLElement>(".modal-backdrop")!.click()],
  ["escape", () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }))],
])("dismisses the confirm sheet on %s without cancelling anything", async (_name, dismiss) => {
  const cancelReservations = vi.fn(async () => ({ cancelled: true, pending: [] }));
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.pending = [{ reservationId: "TEST", trainInfo: "KTX 015 서울 → 부산", expiresAt: null, seatNumber: null }];
  const { app, root } = await mountLive({ bootstrap: async () => state, cancelReservations });
  app.navigate("activity");

  root.querySelector<HTMLButtonElement>("[data-action='cancel-pending']")!.click();
  expect(root.querySelector(".action-sheet")?.textContent).toContain("결제를 기다리는 예약을 모두 취소할까요?");

  dismiss(root);
  expect(root.querySelector(".action-sheet")).toBeNull();
  expect(cancelReservations).not.toHaveBeenCalled();
});

it("offers the access request on the train list, where the error message points to it", async () => {
  const search = vi.fn().mockResolvedValue({ started: false, needsAccessRequest: true });
  const { app, root } = await mountLive({ search });

  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-immediate-any]")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-immediate-any]")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();

  await vi.waitFor(() => expect(root.querySelector("[data-action='request-access']")).not.toBeNull());
  expect(root.querySelector("[data-action='trains-next'], .train-list")).not.toBeNull();
});

it("leaves the cars at each end out of a bulk apply when asked", async () => {
  const seatInventories = vi.fn(async () => ({
    inventories: [3, 4, 5].map((carNo) => makeCarInventory(carNo)),
    failedCars: [5] as number[],
    layoutReference: false,
  }));
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: seatInventoryFake, seatInventories });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='cars:+']")!.click();
  expect(root.querySelector("[data-seat-filter='cars:+']")!.hasAttribute("disabled")).toBe(true); // 3 cars allow at most 1
  expect(root.querySelector("[data-seat-filter='cars:+']")!.parentElement!.querySelector("output")!.textContent).toBe("1개");
  // The cars that count leaves out are marked at once, not only after "모든 호차에 적용".
  expect([...root.querySelectorAll(".car-tab.trimmed b")].map((tab) => tab.textContent)).toEqual(["3호차", "5호차"]);
  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

  await vi.waitFor(() => expect(root.querySelector("[data-seat-car='4'] em")?.textContent).toBe("2"));
  expect(root.querySelector("[data-seat-car='5'] em")).toBeNull(); // the last car was left out
  expect(root.textContent).not.toContain("5호차 좌석표를 불러오지 못해"); // and its failure is not reported either
});

it("lists the running search's trains and seats on the activity screen", async () => {
  const { app, root } = await mountLive();
  root.querySelector<HTMLButtonElement>("nav [data-view='activity']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".activity-conditions")).not.toBeNull());
  const rows = [...root.querySelectorAll(".activity-trains li")].map((row) => row.textContent);
  expect(rows).toEqual(["07:27→10:12 KTX일반실 3호차 5A·5B", "08:03→10:48 KTX좌석 무관"]);
  expect(root.querySelector(".activity-conditions")?.textContent).toContain("KTX 계열만 · 2편 선택");
  expect(root.querySelector(".activity-card .notice")).toBeNull();
  expect(app).toBeTruthy();
});

it("labels a chip with the favourite's name when it matches the restored search", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running = null;
  state.favourites = [{ id: "fav-same", name: "저녁에 부산", route: "서울 → 부산", window: "07:00–12:00", conditions: { ...state.draft!, dep_date: "" } }];
  const { root } = await mountLive({ bootstrap: async () => state });

  const chips = [...root.querySelectorAll("[data-route-chip]")];
  expect(chips).toHaveLength(1);
  expect(chips[0]!.getAttribute("data-route-chip")).toBe("fav-same");
  expect(chips[0]!.textContent).toContain("저녁에 부산");
});

it("sums a whole-formation seat plan instead of listing every label", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running!.seatPlan = { trains: [{ trainNo: "063", label: "063", seatClass: "general", targets: Array.from({ length: 13 }, (_, i) => ({ carNo: i + 1, labels: ["1A", "1D", "2A", "2D"] })) }] };
  const { root } = await mountLive({ bootstrap: async () => state });
  root.querySelector<HTMLButtonElement>("nav [data-view='activity']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".activity-trains li")).not.toBeNull());
  expect(root.querySelector(".activity-trains li")?.textContent).toBe("열차 063일반실 13개 호차 · 52석");
});

it("prints the server's worded seat preference as it is, and hides '지정 없음'", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running!.seatPreference = "창측 3–10번";
  const { root } = await mountLive({ bootstrap: async () => state });
  root.querySelector<HTMLButtonElement>("nav [data-view='activity']")!.click();
  await vi.waitFor(() => expect(root.querySelector(".activity-conditions")).not.toBeNull());
  expect(root.querySelector(".activity-conditions")?.textContent).toContain("좌석 지정창측 3–10번");

  state.running!.seatPreference = "지정 없음";
  const plain = await mountLive({ bootstrap: async () => state });
  plain.root.querySelector<HTMLButtonElement>("nav [data-view='activity']")!.click();
  await vi.waitFor(() => expect(plain.root.querySelector(".activity-conditions")).not.toBeNull());
  expect(plain.root.querySelector(".activity-conditions")?.textContent).not.toContain("좌석 지정");
});

const seatTarget = (carNo: number, label: string): SeatTarget => ({
  carNo, seatNo: `${carNo}-${label}`, label, row: Number(label.slice(0, -1)), column: label.slice(-1),
  direction: "", floor: "", adjacencyGroup: "", position: 0, rowPosition: 0,
});

it("asks before 바로 예약 starts a real reservation, and does nothing when dismissed", async () => {
  const search = vi.fn().mockResolvedValue({ started: true });
  const { app, root } = await mountLive({ search });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-immediate-any]")).not.toBeNull());

  root.querySelector<HTMLButtonElement>("[data-immediate-any]")!.click();
  const sheet = root.querySelector<HTMLElement>(".action-sheet")!;
  expect(sheet.textContent).toContain("바로 예약할까요?");
  expect(sheet.textContent).toContain("09:00→11:42 KTX · 일반실 우선 · 1명");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-cancel']")!.click();
  expect(search).not.toHaveBeenCalled();

  root.querySelector<HTMLButtonElement>("[data-immediate-any]")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(search).toHaveBeenCalledOnce());
});

it("asks before 코레일 예약 대기 is submitted, and does nothing when dismissed", async () => {
  const search = vi.fn().mockResolvedValue({ started: false, waitlisted: true, trainNo: "015" });
  const { app, root } = await mountLive({ search });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-official-waitlist='015']")).not.toBeNull());

  root.querySelector<HTMLButtonElement>("[data-official-waitlist='015']")!.click();
  expect(root.querySelector(".action-sheet")?.textContent).toContain("코레일 예약 대기를 신청할까요?");
  expect(root.querySelector(".action-sheet")?.textContent).toContain("07:27→10:12 KTX · 일반실만 · 1명");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-cancel']")!.click();
  expect(search).not.toHaveBeenCalled();
});

it("asks before deleting a favourite, naming it, and keeps it when dismissed", async () => {
  const deleteFavourite = vi.fn(async () => ({ deleted: true, favourites: [] }));
  const { app, root } = await mountLive({ deleteFavourite });
  app.navigate("favourites");

  root.querySelector<HTMLButtonElement>("[data-delete-favourite]")!.click();
  const sheet = root.querySelector<HTMLElement>(".action-sheet")!;
  expect(sheet.textContent).toContain("즐겨찾기를 삭제할까요?");
  expect(sheet.textContent).toContain("주말에 집으로 · 서울 → 부산");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-cancel']")!.click();
  expect(deleteFavourite).not.toHaveBeenCalled();

  root.querySelector<HTMLButtonElement>("[data-delete-favourite]")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(deleteFavourite).toHaveBeenCalledOnce());
});

it("explains a home problem state and marks it with a glyph instead of the word 확인", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running!.health = "error";
  const { root } = await mountLive({ bootstrap: async () => state });
  const card = root.querySelector<HTMLElement>("[data-search-status]")!;

  expect(card.dataset.state).toBe("error");
  expect(card.querySelector(".search-status-icon")?.textContent).toBe("!");
  expect(card.querySelector(".search-status-description")?.textContent).toBe("자리 찾기는 남아 있지만 현재 조회 상태를 확인할 수 없어요.");
  expect(card.querySelector("[data-view='activity']")?.textContent).toContain("자세히 보기");
});

it("keeps the seat sheet's confirm button out of reach until something is selected", async () => {
  const demo = createDemoApi();
  const { app, root } = await mountLive({ seatCars: demo.seatCars, seatInventory: demo.seatInventory });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-filter='col:A']")).not.toBeNull());

  const confirm = () => root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!;
  expect(confirm().textContent).toBe("0석 중 빈자리 나면 예약");
  expect(confirm().disabled).toBe(true);
  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  expect(confirm().disabled).toBe(false);
});

it("keeps the immediate seat sheet's confirm button out of reach until the party is seated", async () => {
  const demo = createDemoApi();
  const { app, root } = await mountLive({ seatCars: demo.seatCars, seatInventory: demo.seatInventory });
  app.navigate("journey");
  edit(root, "seat_grade_mode", "specific");
  root.querySelector<HTMLInputElement>("[name='seat_class'][value='general']")!.click();
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='025'][data-seat-mode='immediate']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='025'][data-seat-mode='immediate']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-no='demo-1-A']")).not.toBeNull());

  const confirm = () => root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!;
  expect(confirm().disabled).toBe(true);
  root.querySelector<HTMLButtonElement>("[data-seat-no='demo-1-A']")!.click();
  expect(confirm().disabled).toBe(false);
});

describe("확인 화면의 좌석 지정", () => {
  const withPlan = async (trains: { trainNo: string; seatClass: "general"; targets: SeatTarget[] }[] | null) => {
    const demo = createDemoApi();
    const state = await demo.bootstrap();
    state.draft!.seat_preference = "";
    state.draft!.seat_plan = trains ? { strategy: "independent", passengerCount: 1, trains } : undefined;
    const { app, root } = await mountLive({ bootstrap: async () => state });
    app.navigate("confirm");
    return root.querySelector<HTMLElement>(".summary-card")!;
  };

  it("names the picked seats rather than the unset preference", async () => {
    const summary = await withPlan([{ trainNo: "015", seatClass: "general", targets: [seatTarget(3, "5A"), seatTarget(3, "5B")] }]);
    expect(summary.textContent).toContain("좌석 지정3호차 5A·5B");
    expect(summary.textContent).not.toContain("지정 없음");
  });

  it("sums a whole-formation plan instead of listing every label", async () => {
    const summary = await withPlan([{
      trainNo: "015", seatClass: "general",
      targets: Array.from({ length: 13 }, (_, car) => ["1A", "1D", "2A", "2D"].map((label) => seatTarget(car + 1, label))).flat(),
    }]);
    expect(summary.textContent).toContain("좌석 지정1편 · 13개 호차 · 52석");
  });

  it("says 좌석 무관 for a plan of whole trains, and drops the row with no plan at all", async () => {
    expect((await withPlan([{ trainNo: "015", seatClass: "general", targets: [] }])).textContent).toContain("좌석 지정좌석 무관");
    expect((await withPlan(null)).textContent).not.toContain("좌석 지정");
  });
});

it("follows the system theme while none was chosen, and stops once one is", async () => {
  const listeners = new Set<(event: MediaQueryListEvent) => void>();
  const query = {
    matches: false,
    addEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
  };
  Object.defineProperty(window, "matchMedia", { value: () => query, configurable: true });
  onTestFinished(() => {
    Object.defineProperty(window, "matchMedia", { value: undefined, configurable: true });
    window.localStorage.removeItem("jari.theme");
  });
  const flip = (matches: boolean) => {
    for (const listener of listeners) listener({ matches } as MediaQueryListEvent);
  };

  const { app } = await mountLive();
  expect(document.documentElement.dataset.theme).toBe("light");

  flip(true);
  expect(document.documentElement.dataset.theme).toBe("dark");
  flip(false);
  expect(document.documentElement.dataset.theme).toBe("light");

  // 사용자가 한 번 고르면 시스템이 바뀌어도 그대로 둬요.
  window.localStorage.setItem("jari.theme", "light");
  flip(true);
  expect(document.documentElement.dataset.theme).toBe("light");

  app.dispose();
  expect(listeners.size).toBe(0);
});

it("puts the last check on the home 찾는 중 badge as a relative time", async () => {
  const { root } = await mountLive();
  expect(root.querySelector(".idle-badge")?.textContent).toBe("찾는 중 · 방금");

  // 시각이 없으면 배지는 상태만 말해요.
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  state.running!.lastCheckedAt = null;
  const bare = await mountLive({ bootstrap: async () => state });
  expect(bare.root.querySelector(".idle-badge")?.textContent).toBe("찾는 중");
});

it("redraws the home badge on each status poll", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  const status = vi.fn(async () => ({ running: state.running, scheduled: null, pending: [] }));
  const { root } = await mountLive({ bootstrap: async () => state, status });
  expect(root.querySelector(".idle-badge")?.textContent).toBe("찾는 중 · 방금");

  // 폴링이 30초를 밀고 나면 90초 전이 된다. 2분을 넘기면 "확인 지연" 카드로 바뀌므로 그 안쪽을 쓴다.
  state.running!.lastCheckedAt = new Date(Date.now() - 60_000).toISOString();
  await vi.advanceTimersByTimeAsync(30_000);
  await vi.waitFor(() => expect(status).toHaveBeenCalled());
  expect(root.querySelector(".idle-badge")?.textContent).toBe("찾는 중 · 1분 전");
});

it("names the stepper units instead of an unqualified 없음", async () => {
  const demo = createDemoApi();
  const { app, root } = await mountLive({ seatCars: demo.seatCars, seatInventory: demo.seatInventory });
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-filter='trim:+']")).not.toBeNull());

  const trim = () => root.querySelector("[data-seat-filter='trim:+']")!.parentElement!.querySelector("output")!.textContent;
  expect(trim()).toBe("0줄");
  root.querySelector<HTMLButtonElement>("[data-seat-filter='trim:+']")!.click();
  expect(trim()).toBe("1줄");
});

it("keeps a single seat button when the cabin does not matter", async () => {
  const { app, root } = await mountLive();
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());

  const wait = root.querySelector("[data-train-no='015'][data-seat-class='general']");
  expect(wait?.textContent).toContain("좌석 지정");
  expect(root.querySelector("[data-train-no='015'][data-seat-class='special']")).toBeNull();
  expect(root.querySelector("[data-immediate-any='025']")?.textContent).toContain("바로 예약");
  expect(root.querySelector("[data-train-no='025'][data-seat-class]")).toBeNull();
});

it("says what each train-list button will do", async () => {
  const { app, root } = await mountLive();
  app.navigate("journey");
  edit(root, "seat_grade_mode", "specific");
  root.querySelector<HTMLInputElement>("[name='seat_class'][value='general']")!.click();
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='025'][data-seat-class='general']")).not.toBeNull());

  expect(root.querySelector("[data-train-no='025'][data-seat-class='general']")?.textContent).toContain("일반실 · 지금 예약할 좌석");
  expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")?.textContent).toContain("좌석 지정");
  expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")?.textContent).not.toContain("기다릴 좌석");
  expect(root.querySelector("[data-action='trains-next']")?.textContent).toContain("다음: 조건 확인");
  root.querySelector<HTMLButtonElement>("[data-train-toggle='015']")!.click();
  expect(root.querySelector("[data-action='trains-next']")?.textContent).toContain("1편 선택 · 다음: 조건 확인");
});

it("shows the whole-train pick as a check mark that matches aria-pressed", async () => {
  const { app, root } = await mountLive();
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-toggle='015']")).not.toBeNull());
  expect(root.textContent).toContain("카드를 누르면 그 열차의 어떤 자리든 기다려요.");

  const card = () => root.querySelector<HTMLButtonElement>("[data-train-toggle='015']")!;
  expect(card().querySelector(".train-check")).not.toBeNull();
  expect(card().getAttribute("aria-pressed")).toBe("false");

  card().click();
  expect(card().getAttribute("aria-pressed")).toBe("true");
  expect(card().querySelector(".train-check")).not.toBeNull();
});

describe("날짜 시트", () => {
  const openDateSheet = async () => {
    const trains = vi.fn(async () => ({ trains: [], truncated: false, passengerCount: 1 }));
    const mounted = await mountLive({ trains });
    mounted.root.querySelector<HTMLButtonElement>("[data-route-chip='demo-home']")!.click();
    await vi.waitFor(() => expect(mounted.root.querySelector("#sheet-date")).not.toBeNull());
    return { ...mounted, trains };
  };

  it("offers 오늘 and 내일 as chips that drive the date input", async () => {
    const { root } = await openDateSheet();
    const input = () => root.querySelector<HTMLInputElement>("#sheet-date")!;
    const chips = () => [...root.querySelectorAll<HTMLButtonElement>("[data-sheet-date]")];
    expect(chips().map((chip) => chip.textContent)).toEqual(["오늘", "내일"]);

    chips()[1]!.click();
    expect(input().value).toBe(chips()[1]!.dataset.sheetDate);
    expect(chips()[1]!.getAttribute("aria-pressed")).toBe("true");
    expect(chips()[0]!.getAttribute("aria-pressed")).toBe("false");

    // 날짜를 직접 고르면 어느 칩도 선택으로 남지 않아요.
    input().value = "2030-01-01";
    input().dispatchEvent(new Event("change", { bubbles: true }));
    expect(chips().every((chip) => chip.getAttribute("aria-pressed") === "false")).toBe(true);
  });

  it("sends 조건 수정 to the journey form with the saved conditions, searching nothing", async () => {
    const { root, trains } = await openDateSheet();
    root.querySelector<HTMLButtonElement>("[data-action='sheet-edit']")!.click();

    await vi.waitFor(() => expect(root.querySelector("#conditions-form")).not.toBeNull());
    expect(root.querySelector(".action-sheet")).toBeNull();
    expect(trains).not.toHaveBeenCalled();
    expect(root.querySelector<HTMLInputElement>("[name='dep_time']")!.value).toBe("14:00");
    expect(root.querySelector<HTMLInputElement>("[name='max_dep_time']")!.value).toBe("18:00");
  });
});

it("counts the payment deadline down every second without redrawing the screen", async () => {
  vi.useFakeTimers({ now: new Date("2026-09-23T12:00:00Z"), toFake: ["Date", "setInterval", "clearInterval"] });
  const state = await createDemoApi().bootstrap();
  state.pending = [{ reservationId: "TEST", trainInfo: "KTX 015 서울 → 부산", expiresAt: "2026-09-23T12:05:02Z", seatNumber: null }];
  const { root } = await mountLive({ bootstrap: async () => state });
  const left = root.querySelector<HTMLElement>("[data-deadline]")!;
  expect(left.textContent).toBe("5:02 남음");
  expect(left.classList.contains("urgent")).toBe(false);

  vi.advanceTimersByTime(3_000);
  expect(root.querySelector("[data-deadline]")).toBe(left);
  expect(left.textContent).toBe("4:59 남음");
  expect(left.classList.contains("urgent")).toBe(true);
});

it("opens the tabs without a back button and keeps logout last in settings", async () => {
  const { app, root } = await mountLive();
  for (const view of ["activity", "favourites", "settings"] as const) {
    app.navigate(view);
    expect(root.querySelector(".subhead"), view).toBeNull();
  }
  const buttons = root.querySelectorAll(".screen-settings button");
  expect(buttons[buttons.length - 1]?.getAttribute("data-action")).toBe("app-logout");
});

describe("테마 날짜 선택기와 토스트", () => {
  it("replaces the native date input in the journey form and keeps the picked day through rerenders", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-14T10:00:00+09:00"));
    const { app, root } = await mountLive();
    app.navigate("journey");
    expect(root.querySelector("input[type='date']")).toBeNull();

    root.querySelector<HTMLButtonElement>("[data-dp='toggle']")!.click();
    root.querySelector<HTMLButtonElement>("[data-dp-day='2026-09-20']")!.click();
    root.querySelector<HTMLButtonElement>("[data-action='passenger-plus']")!.click();
    app.notify("background");

    expect(root.querySelector<HTMLInputElement>("[name='dep_date']")!.value).toBe("2026-09-20");
    expect(root.querySelector("[data-dp='toggle']")?.textContent).toContain("9월 20일 (일)");
  });

  it("moves the date sheet's calendar with the 오늘·내일 chips", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-30T09:00:00+09:00"));
    const { root } = await mountIdle();
    root.querySelector<HTMLButtonElement>("[data-route-chip='recent']")!.click();
    expect(root.querySelector(".action-sheet input[type='date']")).toBeNull();

    root.querySelector<HTMLButtonElement>("[data-sheet-date='2026-10-01']")!.click();
    expect(root.querySelector(".action-sheet h3")?.textContent).toBe("2026년 10월");
    root.querySelector<HTMLButtonElement>("[data-dp-day='2026-10-09']")!.click();
    expect(root.querySelector<HTMLInputElement>("#sheet-date")!.value).toBe("2026-10-09");
    expect([...root.querySelectorAll("[data-sheet-date][aria-pressed='true']")]).toHaveLength(0);
  });

  it("continues the toast's entrance instead of replaying it when the screen is redrawn", async () => {
    const { app, root } = await mountLive();
    vi.useFakeTimers();
    app.notify("즐겨찾기에 저장했어요.");
    const first = root.querySelector(".toast")!;
    expect(first.getAttribute("style")).toBe("--toast-age: -0ms");

    await vi.advanceTimersByTimeAsync(1_000);
    app.navigate("activity");
    const redrawn = root.querySelector(".toast")!;
    expect(redrawn).not.toBe(first);
    expect(Number(/-(\d+)ms/.exec(redrawn.getAttribute("style")!)![1])).toBeGreaterThanOrEqual(1_000);
    expect(redrawn.classList.contains("leaving")).toBe(false);
  });

  it("stays at least four seconds, then fades out and empties", async () => {
    const { app, root } = await mountLive();
    vi.useFakeTimers();
    app.notify("저장했어요.");
    await vi.advanceTimersByTimeAsync(3_900);
    expect(root.querySelector(".toast")?.textContent).toBe("저장했어요.");
    // 4초 + 글자당 60ms 뒤에 사라지기 시작해요.
    await vi.advanceTimersByTimeAsync(500);
    expect(root.querySelector(".toast")?.classList.contains("leaving")).toBe(true);
    await vi.advanceTimersByTimeAsync(200);
    expect(root.querySelector(".toast")?.textContent).toBe("");
    expect(app).toMatchObject({ toast: "" });
  });

  it("dismisses on tap", async () => {
    const { app, root } = await mountLive();
    vi.useFakeTimers();
    app.notify("저장했어요.");
    root.querySelector<HTMLElement>(".toast")!.click();
    await vi.advanceTimersByTimeAsync(200);
    expect(root.querySelector(".toast")?.textContent).toBe("");
  });

  it("rises above a sticky action button and sits right over the tab bar elsewhere", async () => {
    const { app, root } = await mountLive();
    app.notify("저장했어요.");
    expect(root.querySelector(".toast")?.classList.contains("above-action")).toBe(false);
    app.navigate("trains");
    expect(root.querySelector(".screen .sticky-action")).not.toBeNull();
    expect(root.querySelector(".toast")?.classList.contains("above-action")).toBe(true);
    const css = await readFile("src/styles.css", "utf8");
    expect(css).toContain(".toast.above-action { --toast-lift: calc(var(--nav-clearance) + 54px); }");
    expect(css).toContain("bottom: calc(var(--nav-height) + var(--toast-shell-gap, 0px) + 10px + var(--toast-lift));");
  });
});

it("says the rest of a split booking is still being searched only while the search is healthy", async () => {
  for (const [health, expected] of [["healthy", "1/2석 확보 · 나머지 찾는 중"], ["error", "1/2석 확보 · 나머지 찾기: 조회 문제"]] as const) {
    const state = await createDemoApi().bootstrap();
    state.running!.passengerCount = 2;
    state.running!.health = health;
    state.running!.lastCheckedAt = new Date().toISOString();
    state.pending = [{ reservationId: "TEST", trainInfo: "KTX 015 서울 → 부산", expiresAt: null, seatNumber: null }];
    const { root } = await mountLive({ bootstrap: async () => state });
    expect(root.querySelector(".payment-card .card-label")?.textContent).toContain(expected);
  }
});

it("closes an open sheet on Android back before leaving the screen", async () => {
  const { app, root } = await mountIdle();
  app.navigate("favourites");
  app.navigate("home");
  root.querySelector<HTMLButtonElement>("[data-route-chip='recent']")!.click();
  expect(root.querySelector(".action-sheet")).not.toBeNull();

  expect(app.back()).toBe(true);
  expect(root.querySelector(".action-sheet")).toBeNull();
  expect(root.querySelector(".screen-home")).not.toBeNull();

  expect(app.back()).toBe(true);
  expect(root.querySelector(".screen-favourites")).not.toBeNull();
});
