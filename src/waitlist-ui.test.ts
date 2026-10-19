import { afterEach, expect, it, vi } from "vitest";

import { JariApp } from "./app";
import { createDemoApi } from "./demo";
import type { BootstrapState, PendingReservation, WaitlistEntry } from "./types";

// 코레일 예약 대기: 매진 열차에서 먼저 권하고, 신청한 대기는 홈에 상태별로 보여요.
let app: JariApp | undefined;
afterEach(() => {
  app?.dispose();
  app = undefined;
  document.body.replaceChildren();
});

async function mount(overrides: Partial<ReturnType<typeof createDemoApi>> = {}) {
  const api = { ...createDemoApi(), ...overrides };
  const root = document.createElement("div");
  document.body.append(root);
  app = new JariApp(root, api, { demoMode: false });
  await app.start(true);
  return { app, root, api };
}

function entry(state: WaitlistEntry["state"], changes: Partial<WaitlistEntry> = {}): WaitlistEntry {
  return {
    id: `W-${state}`,
    trainNo: "015",
    trainInfo: "KTX 015 서울 → 부산 · 10월 22일(목) 07:27→10:12",
    depDate: "20261022",
    depTime: "072700",
    passengerCount: 1,
    state,
    createdAt: new Date().toISOString(),
    checkedAt: null,
    checkFailed: false,
    ...changes,
  };
}

async function withWaitlists(waitlists: WaitlistEntry[], pending: PendingReservation[] = []): Promise<BootstrapState> {
  const state = await createDemoApi().bootstrap();
  return { ...state, waitlists, pending, waitlistCheckMinutes: 10 };
}

async function listTrains(root: HTMLElement, app: JariApp): Promise<void> {
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector(".screen-trains")).not.toBeNull());
}

it("puts 코레일 예약 대기 first on a sold-out train that takes one, and says what it cannot do", async () => {
  const { app, root } = await mount();
  await listTrains(root, app);

  const card = root.querySelector("[data-official-waitlist='015']")!.closest(".train-card")!;
  const actions = [...card.querySelectorAll(".train-actions > *")];
  expect(actions[0]!.matches("[data-official-waitlist='015']")).toBe(true);
  expect(actions[0]!.textContent).toBe("코레일 예약 대기 신청");
  // 취소표 대기도 남아 있어 특실·좌석 조건이 필요하면 고를 수 있어요.
  expect(card.querySelector("[data-seat-map]")).not.toBeNull();
  // 대기 대상이 아닌 매진 열차는 지금처럼 취소표 대기만 있어요.
  const other = root.querySelector("[data-train-card='019']")!;
  expect(other.querySelector("[data-official-waitlist]")).toBeNull();
  expect(other.querySelector("[data-seat-map]")).not.toBeNull();
  const advice = root.querySelector(".waitlist-advice")!.textContent!;
  expect(advice).toContain("일반실만");
  expect(advice).toContain("좌석 위치·방향·범위는 고를 수 없어요");
});

it("does not offer 코레일 예약 대기 when only 특실 is wanted, and does not call an unknown flag eligible", async () => {
  const demo = createDemoApi();
  const trains = vi.fn(async () => ({
    trains: [
      { no: "015", label: "07:27→10:12 KTX", trainKey: "k15", soldout: true, waitlistEligible: true, generalAvailable: false, specialAvailable: false },
      { no: "017", label: "07:40→10:20 KTX", trainKey: "k17", soldout: true, waitlistEligible: null, generalAvailable: false, specialAvailable: false },
    ],
    truncated: false,
    passengerCount: 1,
  }));
  const { app, root } = await mount({ ...demo, trains });
  app.navigate("journey");
  root.querySelector<HTMLInputElement>("[name='seat_grade_mode'][value='specific']")!.click();
  root.querySelector<HTMLInputElement>("[name='seat_class'][value='special']")!.click();
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector(".screen-trains")).not.toBeNull());

  expect(root.querySelector("[data-official-waitlist]")).toBeNull();
  expect(root.querySelector(".waitlist-advice")).toBeNull();
});

it("recommends 코레일 예약 대기 on the confirm screen of a watch over a train that takes one", async () => {
  const { app, root } = await mount();
  await listTrains(root, app);
  root.querySelector<HTMLButtonElement>("[data-train-toggle='015']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='trains-next']")!.click();

  await vi.waitFor(() => expect(root.querySelector(".screen-confirm")).not.toBeNull());
  expect(root.querySelector(".waitlist-advice")!.textContent).toContain("1편은 코레일 예약 대기가 돼요");
});

it("shows each standby on the home screen by its state, apart from seats awaiting payment", async () => {
  const state = await withWaitlists([
    entry("waiting", { checkedAt: new Date().toISOString() }),
    entry("waiting", { id: "W-failed", checkFailed: true }),
    entry("waiting", { id: "W-login", checkFailed: true, checkError: "login" }),
    entry("assigned", { seatLabels: ["3호차 7A"], expiresAt: new Date(Date.now() + 3_600_000).toISOString() }),
    entry("ended"),
    entry("cancelled"),
  ]);
  const { root } = await mount({ bootstrap: async () => state });

  const card = (id: string) => root.querySelector(`[data-waitlist='${id}']`)!;
  expect(card("W-waiting").textContent).toContain("코레일 예약 대기 중");
  expect(card("W-waiting").textContent).toContain("방금 확인");
  expect(card("W-waiting").querySelector("[data-waitlist-cancel='W-waiting']")).not.toBeNull();
  expect(card("W-failed").textContent).toContain("마지막 확인에 실패했어요");
  expect(card("W-login").textContent).toContain("계정을 다시 연결해 주세요");
  expect(card("W-assigned").textContent).toContain("좌석 배정됨 · 결제 필요");
  expect(card("W-assigned").textContent).toContain("3호차 7A");
  expect(card("W-assigned").querySelector("[data-korail-pay]")).not.toBeNull();
  expect(card("W-ended").textContent).toContain("예약 대기 끝남");
  expect(card("W-cancelled").textContent).toContain("취소함");
  // 신청한 대기는 좌석 확보가 아니에요.
  expect(root.querySelector(".payment-card")).toBeNull();
  expect(root.querySelector(".idle-quiet")).toBeNull();
});

it("leaves an assigned standby to the payment card once it became the pending payment", async () => {
  const pending: PendingReservation = {
    reservationId: "W-assigned",
    trainInfo: "KTX 015 서울 → 부산 · 10월 22일(목) 07:27→10:12",
    expiresAt: new Date(Date.now() + 3_600_000).toISOString(),
    seatNumber: null,
    seatLabels: ["3호차 7A"],
    seatClass: "general",
  };
  const state = await withWaitlists([entry("assigned", { seatLabels: ["3호차 7A"] })], [pending]);
  const { root } = await mount({ bootstrap: async () => state });

  expect(root.querySelector("[data-waitlist]")).toBeNull();
  expect(root.querySelector(".payment-card")!.textContent).toContain("코레일 예약 대기 배정");
});

it("withdraws a standby only after asking, and shows what the server says is left", async () => {
  const state = await withWaitlists([entry("waiting")]);
  const cancelWaitlist = vi.fn(async () => ({ cancelled: true, waitlists: [entry("cancelled", { id: "W-waiting" })] }));
  const { root } = await mount({ bootstrap: async () => state, cancelWaitlist });

  root.querySelector<HTMLButtonElement>("[data-waitlist-cancel='W-waiting']")!.click();
  expect(root.querySelector(".action-sheet")!.textContent).toContain("코레일 예약 대기를 취소할까요?");
  root.querySelector<HTMLButtonElement>("[data-action='sheet-cancel']")!.click();
  expect(cancelWaitlist).not.toHaveBeenCalled();

  root.querySelector<HTMLButtonElement>("[data-waitlist-cancel='W-waiting']")!.click();
  root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
  await vi.waitFor(() => expect(cancelWaitlist).toHaveBeenCalledWith("W-waiting"));
  await vi.waitFor(() => expect(root.querySelector("[data-waitlist='W-waiting']")!.textContent).toContain("취소함"));
});

it("takes the standbys from each status poll", async () => {
  const state = await withWaitlists([entry("waiting")]);
  const demo = createDemoApi();
  const status = vi.fn(async () => ({ running: null, scheduled: null, pending: [], waitlists: [entry("ended", { id: "W-waiting" })] }));
  const { app, root } = await mount({ ...demo, bootstrap: async () => state, status });

  await (app as unknown as { pollStatus(fresh: boolean): Promise<void> }).pollStatus(true);

  await vi.waitFor(() => expect(root.querySelector("[data-waitlist='W-waiting']")!.textContent).toContain("예약 대기 끝남"));
});
