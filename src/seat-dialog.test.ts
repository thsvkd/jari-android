import { afterEach, describe, expect, it, vi } from "vitest";

import { JariApp } from "./app";
import { ApiError } from "./api";
import { createDemoApi } from "./demo";
import type { SeatInventory, SeatMapSeat } from "./types";

const mounted: JariApp[] = [];

afterEach(() => {
  for (const app of mounted.splice(0)) app.dispose();
  document.body.replaceChildren();
});

async function mountLive(overrides: Partial<ReturnType<typeof createDemoApi>> = {}) {
  const api = { ...createDemoApi(), ...overrides };
  const root = document.createElement("div");
  document.body.append(root);
  const app = new JariApp(root, api, { demoMode: false });
  mounted.push(app);
  await app.start(true);
  return { app, root };
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => { resolve = yes; });
  return { promise, resolve };
}

// 4 rows (not 2) so maxTrimRows > 0 and the trim stepper isn't disabled by its own boundary — only by the bulk-apply lock, in the tests that check that.
function makeCarInventory(carNo: number, salePossible = true, rows = 4): SeatInventory {
  const seat = (row: number, column: "A" | "B"): SeatMapSeat => ({
    carNo, seatNo: `${carNo}-${row}-${column}`, label: `${row}${column}`,
    salePossible, direction: "1", floor: "", row, column,
    adjacencyGroup: `${carNo}:${row}`, position: column === "A" ? 1 : 2, rowPosition: column === "A" ? 1 : 2, familyLabel: "",
  });
  const seats = Array.from({ length: rows }, (_, index) => index + 1).flatMap((row) => [seat(row, "A"), seat(row, "B")]);
  return { carNo, layoutType: 2, arrangementCode: "4", remainingCount: seats.length, totalCount: seats.length, seats };
}

const threeCarSeatCars = async () => ({
  cars: [3, 4, 5].map((carNo) => ({ carNo, roomClassName: "일반실", remainingSeatCount: 4, attributes: [] })),
});

async function openThreeCarWaitDialog(root: HTMLElement, app: JariApp) {
  app.navigate("journey");
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-action='apply-all-cars']")).not.toBeNull());
}

describe("seat dialog selectability and window detection", () => {
  it("disables a for-sale-looking seat when the layout is only a reference from another day", async () => {
    const demo = createDemoApi();
    const { app, root } = await mountLive({
      seatCars: demo.seatCars,
      seatInventory: async (...args) => ({ ...(await demo.seatInventory(...args)), layoutReference: true }),
    });
    app.navigate("journey");
    edit(root, "seat_grade_mode", "specific");
    root.querySelector<HTMLInputElement>("[name='seat_class'][value='general']")!.click();
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-train-no='025'][data-seat-mode='immediate']")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("[data-train-no='025'][data-seat-mode='immediate']")!.click();

    // demo seat "1A" is salePossible in the fixture, so with the old (salePossible-only) selectable() check it would still be clickable here.
    await vi.waitFor(() => expect(root.querySelector("[data-seat-no='demo-1-A']")).not.toBeNull());
    const cell = root.querySelector<HTMLButtonElement>("[data-seat-no='demo-1-A']")!;
    expect(cell.disabled).toBe(true);
    cell.click();

    expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(0);
    expect(root.textContent).toContain("선택한 좌석이 없어요.");
  });

  it("labels a window seat by its column across the car, not by position within a short row", async () => {
    const seat = (row: number, column: "A" | "B" | "C" | "D", group: "left" | "right", position: number): SeatMapSeat => ({
      carNo: 3, seatNo: `${row}-${column}`, label: `${row}${column}`,
      salePossible: true, direction: "1", floor: "", row, column,
      adjacencyGroup: `${row}:${group}`, position, rowPosition: "ABCD".indexOf(column) + 1, familyLabel: "",
    });
    // Row 1 is the full 4-across layout; row 2 is a short row next to a door, so its last seat (B) sits by the aisle, not a window.
    const inventory: SeatInventory = {
      carNo: 3, layoutType: 2, arrangementCode: "4", remainingCount: 6, totalCount: 6,
      seats: [
        seat(1, "A", "left", 1), seat(1, "B", "left", 2), seat(1, "C", "right", 1), seat(1, "D", "right", 2),
        seat(2, "A", "left", 1), seat(2, "B", "left", 2),
      ],
    };
    const { app, root } = await mountLive({
      seatCars: async () => ({ cars: [{ carNo: 3, roomClassName: "일반실", remainingSeatCount: 6, attributes: [] }] }),
      seatInventory: async () => inventory,
    });
    app.navigate("journey");
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-train-no='015'][data-seat-class='general']")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("[data-train-no='015'][data-seat-class='general']")!.click();

    await vi.waitFor(() => expect(root.querySelector("[data-seat-no='2-B']")).not.toBeNull());
    expect(root.querySelector("[data-seat-no='1-D']")!.className).toContain("window-seat");
    expect(root.querySelector("[data-seat-no='1-D'] small")?.textContent).toBe("창가");
    expect(root.querySelector("[data-seat-no='2-B']")!.className).not.toContain("window-seat");
    expect(root.querySelector("[data-seat-no='2-B'] small")?.textContent).not.toBe("창가");
  });
});

describe("bulk apply to all cars locks the dialog for the whole run", () => {
  it("ignores seat taps, filter chips, car tabs and confirm while the batch request is in flight, then applies a consistent result", async () => {
    const batch = deferred<{ inventories: SeatInventory[]; failedCars: number[]; layoutReference: boolean }>();
    const seatInventories = vi.fn(() => batch.promise);
    const { app, root } = await mountLive({
      seatCars: threeCarSeatCars,
      seatInventory: async (_trainKey: string, carNo: number) => makeCarInventory(carNo, false),
      seatInventories,
    });
    await openThreeCarWaitDialog(root, app);

    root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
    expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(4); // one A seat per row (4 rows)
    root.querySelector<HTMLButtonElement>("[data-action='apply-all-cars']")!.click();

    // Mid-flight (the single batch request hasn't resolved yet): everything but closing the dialog is locked.
    await vi.waitFor(() => expect(root.querySelector<HTMLButtonElement>("[data-seat-car='4']")!.disabled).toBe(true));
    expect(root.querySelector<HTMLButtonElement>("[data-seat-no='3-1-A']")!.disabled).toBe(true);
    expect(root.querySelector<HTMLButtonElement>("[data-seat-filter='col:B']")!.disabled).toBe(true);
    expect(root.querySelector<HTMLButtonElement>("[data-seat-filter='trim:+']")!.disabled).toBe(true);
    expect(root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!.disabled).toBe(true);
    expect(root.querySelector("[data-action='apply-all-cars']")!.textContent).toBe("모든 호차 확인 중…");

    // A tap on an already-selected seat (which would normally deselect it) must have no effect while locked.
    root.querySelector<HTMLButtonElement>("[data-seat-no='3-1-A']")!.click();
    root.querySelector<HTMLButtonElement>("[data-seat-filter='col:B']")!.click();
    expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(4);

    batch.resolve({ inventories: [3, 4, 5].map((carNo) => makeCarInventory(carNo, false)), failedCars: [], layoutReference: false });
    await vi.waitFor(() => expect(root.querySelector("[data-seat-car='4'] em")?.textContent).toBe("4"));
    expect(root.querySelector("[data-seat-car='5'] em")?.textContent).toBe("4");
    expect(seatInventories).toHaveBeenCalledTimes(1);
    // The col:A filter tapped before the run started is still what every car was matched against, not "col:A + col:B".
    expect(root.querySelectorAll(".seat-cell.selected")).toHaveLength(4);
    expect(root.querySelector<HTMLButtonElement>("[data-seat-car='4']")!.disabled).toBe(false);
    expect(root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!.disabled).toBe(false);
  });
});

describe("designated reservation conflict caches the fresh inventory", () => {
  it("keeps the just-sold seat unavailable after switching cars away and back, instead of refetching the stale cache", async () => {
    let car3Calls = 0;
    const soldOut = makeCarInventory(3, false);
    const seatInventory = vi.fn(async (_trainKey: string, carNo: number) => {
      if (carNo !== 3) return makeCarInventory(carNo);
      car3Calls += 1;
      return car3Calls === 1 ? makeCarInventory(3) : soldOut;
    });
    const reserveDesignated = vi.fn().mockRejectedValue(new ApiError("seat changed", 409, "server"));
    const { app, root } = await mountLive({
      seatCars: async () => ({ cars: [{ carNo: 3, roomClassName: "일반실", remainingSeatCount: 4, attributes: [] }, { carNo: 4, roomClassName: "일반실", remainingSeatCount: 4, attributes: [] }] }),
      seatInventory,
      reserveDesignated,
    });
    app.navigate("journey");
    edit(root, "seat_grade_mode", "specific");
    root.querySelector<HTMLInputElement>("[name='seat_class'][value='general']")!.click();
    root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
    await vi.waitFor(() => expect(root.querySelector("[data-train-no='025'][data-seat-mode='immediate']")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("[data-train-no='025'][data-seat-mode='immediate']")!.click();
    await vi.waitFor(() => expect(root.querySelector("[data-seat-no='3-1-A']")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("[data-seat-no='3-1-A']")!.click();
    root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!.click();
    // 실제 예약이라 확인 시트에서 한 번 더 눌러요.
    await vi.waitFor(() => expect(root.querySelector("[data-action='sheet-confirm']")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("[data-action='sheet-confirm']")!.click();
    await vi.waitFor(() => expect(root.textContent).toContain("선택한 좌석이 방금 판매됐어요"));
    expect(seatInventory).toHaveBeenCalledTimes(2); // initial car 3 load + the 409 refetch

    root.querySelector<HTMLButtonElement>("[data-seat-car='4']")!.click();
    await vi.waitFor(() => expect(root.querySelector("[data-seat-no='4-1-A']")).not.toBeNull());
    root.querySelector<HTMLButtonElement>("[data-seat-car='3']")!.click();

    // Shown sold (tapping it now starts a wait instead of booking), not as the stale cache's bookable seat.
    await vi.waitFor(() => expect(root.querySelector("[data-seat-no='3-1-A']")!.classList.contains("occupied")).toBe(true));
    expect(seatInventory).toHaveBeenCalledTimes(3); // car 4's load only; car 3 was served from the post-conflict cache
  });
});

it("dims the rows and columns the conditions leave out, whether those seats are sold or not", async () => {
  const inventory = makeCarInventory(3, true, 4);
  inventory.seats[0]!.salePossible = false; // a sold seat inside a row the trim leaves out
  const { app, root } = await mountLive({
    seatCars: threeCarSeatCars,
    seatInventory: async (_trainKey: string, carNo: number) => (carNo === 3 ? inventory : makeCarInventory(carNo)),
  });
  await openThreeCarWaitDialog(root, app);
  expect(root.querySelectorAll(".seat-row.excluded, .seat-cell.excluded")).toHaveLength(0);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='trim:+']")!.click();
  const rows = [...root.querySelectorAll(".seat-row")];
  expect(rows.map((row) => row.classList.contains("excluded"))).toEqual([true, false, false, true]);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  // Kept rows lose their B seat to the column filter; excluded rows stay a whole band rather than per-seat marks.
  expect([...root.querySelectorAll(".seat-cell.excluded")].map((cell) => (cell as HTMLElement).dataset.seatNo)).toEqual(["3-2-B", "3-3-B"]);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='cars:+']")!.click();
  expect([...root.querySelectorAll(".car-tab.trimmed")].map((tab) => (tab as HTMLElement).dataset.seatCar)).toEqual(["3", "5"]);
});

// Demo train 025 has seats for sale; its demo car sells row 1 (demo-1-A..D) and rows 2–4 are sold.
async function openImmediateDialog(passengers = 1) {
  const { app, root } = await mountLive();
  app.navigate("journey");
  edit(root, "seat_grade_mode", "specific");
  root.querySelector<HTMLInputElement>("[name='seat_class'][value='general']")!.click();
  for (let count = 1; count < passengers; count += 1) root.querySelector<HTMLButtonElement>("[data-action='passenger-plus']")!.click();
  root.querySelector<HTMLFormElement>("#conditions-form")!.requestSubmit();
  await vi.waitFor(() => expect(root.querySelector("[data-train-no='025'][data-seat-mode='immediate']")).not.toBeNull());
  root.querySelector<HTMLButtonElement>("[data-train-no='025'][data-seat-mode='immediate']")!.click();
  await vi.waitFor(() => expect(root.querySelector("[data-seat-no='demo-2-A']")).not.toBeNull());
  const tap = (seatNo: string) => root.querySelector<HTMLButtonElement>(`[data-seat-no='demo-${seatNo}']`)!.click();
  const picked = () => [...root.querySelectorAll<HTMLElement>(".seat-cell.selected")].map((cell) => cell.dataset.seatNo!.slice(5));
  const confirm = () => root.querySelector("[data-action='confirm-seat-dialog']")!.textContent;
  const notice = () => root.querySelector(".seat-mode-notice")?.textContent ?? "";
  const mode = () => root.querySelector(".seat-mode [aria-checked='true']")?.textContent;
  return { root, tap, picked, confirm, notice, mode };
}

describe("seat dialog switches between booking now and waiting by the seat tapped", () => {
  it("turns a train with seats into a wait when a sold seat is tapped, letting go of the free seats", async () => {
    const { root, tap, picked, confirm, notice, mode } = await openImmediateDialog(2);
    expect(mode()).toBe("바로 예약");
    tap("1-A");
    tap("1-B");
    expect(confirm()).toBe("2/2석 선택 · 예약하기");
    expect(root.querySelector("#seat-dialog-title")!.textContent).toBe("예약할 좌석을 선택해 주세요");

    tap("2-A");
    expect(picked()).toEqual(["2-A"]);
    expect(confirm()).toBe("1석 중 빈자리 나면 예약");
    expect(root.querySelector<HTMLButtonElement>("[data-action='confirm-seat-dialog']")!.disabled).toBe(false);
    expect(mode()).toBe("취소표 대기");
    expect(notice()).toBe("빈 좌석 2석 선택을 풀었어요");
    expect(root.querySelector(".seat-mode-notice")!.getAttribute("role")).toBe("status");
    expect(root.querySelector("#seat-dialog-title")!.textContent).toBe("좌석 범위를 선택해 주세요");

    // Further sold seats add up and a second tap lets one go, as waiting always did; the notice was for the switch only.
    tap("3-A");
    tap("4-B");
    tap("3-A");
    expect(picked()).toEqual(["2-A", "4-B"]);
    expect(confirm()).toBe("2석 중 빈자리 나면 예약");
    expect(notice()).toBe("");
  });

  it("goes straight back to booking now when a for-sale seat is tapped while waiting", async () => {
    const { tap, picked, confirm, notice, mode } = await openImmediateDialog(2);
    tap("2-A");
    expect(mode()).toBe("취소표 대기");
    expect(notice()).toBe("");
    tap("3-B");

    tap("1-C");
    expect(picked()).toEqual(["1-C"]);
    expect(confirm()).toBe("1/2석 선택 · 예약하기");
    expect(mode()).toBe("바로 예약");
    expect(notice()).toBe("대기 좌석 2석 선택을 풀었어요");
  });

  it("still holds booking now to the passenger count, dropping the oldest pick", async () => {
    const { tap, picked, confirm, notice } = await openImmediateDialog(2);
    tap("1-A");
    tap("1-B");
    tap("1-C");
    expect(picked()).toEqual(["1-B", "1-C"]);
    expect(confirm()).toBe("2/2석 선택 · 예약하기");
    tap("1-C");
    expect(picked()).toEqual(["1-B"]);
    expect(notice()).toBe("");
  });
});

it("lets the wait-mode chips pick only sold seats on a train that still has free ones", async () => {
  const inventory = makeCarInventory(3, false, 4);
  inventory.seats[0]!.salePossible = true; // 3-1-A is for sale
  const { app, root } = await mountLive({
    seatCars: threeCarSeatCars,
    seatInventory: async (_trainKey: string, carNo: number) => (carNo === 3 ? inventory : makeCarInventory(carNo, false)),
  });
  await openThreeCarWaitDialog(root, app);

  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  expect([...root.querySelectorAll(".seat-cell.selected")].map((cell) => (cell as HTMLElement).dataset.seatNo)).toEqual(["3-2-A", "3-3-A", "3-4-A"]);
});

it("lets the switch above the map pick the mode, so the chips pick sold seats to wait for", async () => {
  const { root, picked, confirm, notice, mode } = await openImmediateDialog(1);
  root.querySelector<HTMLButtonElement>("[data-seat-mode-switch='wait']")!.click();
  expect(mode()).toBe("취소표 대기");
  expect(notice()).toBe("");
  root.querySelector<HTMLButtonElement>("[data-seat-filter='col:A']")!.click();
  // 데모 좌석표는 1열만 비어 있어요. 대기로 고르면 A열의 팔린 자리만 골라요.
  expect(picked().every((seat) => !seat.startsWith("1-"))).toBe(true);
  expect(picked().length).toBeGreaterThan(0);
  expect(confirm()).toBe(`${picked().length}석 중 빈자리 나면 예약`);

  root.querySelector<HTMLButtonElement>("[data-seat-mode-switch='immediate']")!.click();
  expect(mode()).toBe("바로 예약");
  expect(picked()).toEqual([]);
  expect(root.querySelector("[data-seat-filter='col:A']")!.getAttribute("aria-pressed")).toBe("false");
});

it("shows no mode switch on a sold-out train, which can only wait", async () => {
  const { app, root } = await mountLive({ seatCars: threeCarSeatCars, seatInventory: async (_trainKey: string, carNo: number) => makeCarInventory(carNo, false) });
  await openThreeCarWaitDialog(root, app);
  expect(root.querySelector(".seat-mode")).toBeNull();
});
