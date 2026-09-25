import type { Page } from "@playwright/test";

import { expect, nextPoll } from "./fixtures";

/** 오늘부터 며칠 뒤의 한국 날짜. 가짜 코레일은 이 날짜로 열차를 만들어요. */
export function travelDay(daysAhead = 3): { iso: string; label: string } {
  const now = new Date(Date.now() + daysAhead * 86_400_000);
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit" })
    .format(now);
  const [year, month, day] = parts.split("-").map(Number);
  const weekday = "일월화수목금토"[new Date(Date.UTC(year!, month! - 1, day!)).getUTCDay()];
  return { iso: parts, label: `${month}월 ${day}일(${weekday})` };
}

/** 앱 알림과 결제 카드에 나와야 하는 열차 한 줄. 가짜 코레일의 첫 열차(07:00 출발) 기준이에요. */
export function trainLine(daysAhead = 3, no = "00101", times = "07:00→09:41"): string {
  return `KTX ${no} 서울 → 부산 · ${travelDay(daysAhead).label} ${times}`;
}

// 날짜 고르기는 한 곳에서만 해요. 날짜 선택 UI 가 바뀌면 여기만 고쳐요.
// 새 여정의 달력은 접힌 날짜 칸을 눌러 펼치고, 그 달에 없으면 다음 달로 넘겨서 날을 눌러요.
export async function pickDate(page: Page, iso: string): Promise<void> {
  const toggle = page.locator("[data-dp='toggle']");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  const day = page.locator(`[data-dp-day='${iso}']:not([disabled])`);
  for (let month = 0; month < 13 && !(await day.isVisible()); month++) {
    await page.locator("[data-dp='next']").click();
  }
  await day.click();
  await expect(page.locator("[name=dep_date]")).toHaveValue(iso);
}

// 시간도 한 곳에서만 골라요. 접힌 시간 칸을 눌러 펼치고, 시 칩과 10분 단위 분 칩을 차례로 눌러요.
// 이미 펼쳐져 있으면 누르지 않아요. 앞서 연 패널은 화면을 오가도 열린 채라, 누르면 도로 접혀요
// (밤 10시가 넘으면 끝 시각 칸이 꺼져 출발 시각 패널을 닫아 주는 쪽이 없어서 드러났어요).
export async function pickTime(page: Page, name: "dep_time" | "max_dep_time", clock: string): Promise<void> {
  const picker = page.locator("[data-time-picker]", { has: page.locator(`[name=${name}]`) });
  const [hour, minute] = clock.split(":").map(Number);
  const toggle = picker.locator("[data-tp='toggle']");
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await picker.locator(`[data-tp-hour='${hour}']`).click();
  await picker.locator(`[data-tp-minute='${minute}']`).click();
  await expect(page.locator(`[name=${name}]`)).toHaveValue(clock);
}

export interface JourneyOptions {
  daysAhead?: number;
  seatMode?: "any" | "specific";
  seatClasses?: Array<"general" | "special">;
}

/** 홈에서 새 여정을 열고 조건을 채워 열차 목록까지 가요. */
export async function searchTrains(page: Page, options: JourneyOptions = {}): Promise<void> {
  await page.locator(".screen-home [data-action='new-journey']").click();
  await expect(page.locator(".screen-journey")).toBeVisible();
  await pickDate(page, travelDay(options.daysAhead ?? 3).iso);
  await pickTime(page, "dep_time", "07:00");
  await page.locator("[name=unlimited_time]").check();
  if (options.seatMode === "specific") {
    // 사용자는 숨은 입력이 아니라 그 카드를 눌러요.
    await page.locator("label", { has: page.locator("[name=seat_grade_mode][value=specific]") }).click();
    for (const seatClass of options.seatClasses ?? ["general"]) {
      await page.locator("label", { has: page.locator(`[name=seat_class][value=${seatClass}]`) }).click();
      await expect(page.locator(`[name=seat_class][value=${seatClass}]`)).toBeChecked();
    }
  }
  await page.getByRole("button", { name: /열차 조회하기/ }).click();
  await expect(page.locator(".screen-trains [data-train-toggle]").first()).toBeVisible();
}

/** 열차 목록에서 이 열차들만 고른 채로 둬요. */
export async function keepOnlyTrains(page: Page, numbers: string[]): Promise<void> {
  for (const toggle of await page.locator("[data-train-toggle]").all()) {
    const no = await toggle.getAttribute("data-train-toggle");
    const pressed = (await toggle.getAttribute("aria-pressed")) === "true";
    if (numbers.includes(no!) !== pressed) await toggle.click();
  }
}

/** 조건 확인에서 "지금 빈자리 찾기"를 눌러 찾기를 시작해요. */
export async function startSearching(page: Page): Promise<void> {
  await page.locator("[data-action='trains-next']").click();
  await expect(page.locator(".screen-confirm")).toBeVisible();
  await page.locator("[data-action='start-now']").click();
  const sheetConfirm = page.locator("[data-action='sheet-confirm']");
  if (await sheetConfirm.isVisible().catch(() => false)) await sheetConfirm.click();
}

/** 예약 워커가 좌석을 잡을 때까지 기다렸다가, 앱의 30초 폴링을 당겨 화면에 반영해요. */
export async function waitForPaymentCard(page: Page): Promise<void> {
  for (let attempt = 0; attempt < 15; attempt++) {
    await page.waitForTimeout(1_000);
    await nextPoll(page);
    if (await page.locator(".payment-card").isVisible()) return;
  }
  await expect(page.locator(".payment-card")).toBeVisible();
}
