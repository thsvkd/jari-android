import { expect, nextPoll, test } from "./fixtures";
import { keepOnlyTrains, searchTrains, trainLine } from "./flows";
import { expectCleanLayout } from "./layout";

// 코레일 공식 예약 대기. 가짜 코레일의 00103 편이 매진이면서 예약 대기 대상(h_wait_rsv_flg " 9")이에요.
// 신청·확인·취소는 운영 코드가 그대로 돌고, 코레일 호출만 fake_korail 이 답해요.

const WAITLIST_TRAIN = "00103";

async function applyForWaitlist(page: import("@playwright/test").Page): Promise<void> {
  await searchTrains(page);
  await page.locator(`[data-official-waitlist='${WAITLIST_TRAIN}']`).click();
  await expect(page.locator(".action-sheet")).toContainText("좌석 위치·방향·범위는 고를 수 없어요");
  await page.locator("[data-action='sheet-confirm']").click();
  await expect(page.locator(".screen-home")).toBeVisible();
}

test.describe("코레일 예약 대기", () => {
  test("예약 대기가 되는 매진 열차는 대기 신청이 첫 행동이고, 한계를 알려요 @layout", async ({ signedIn: page }) => {
    await searchTrains(page);
    const card = page.locator(`[data-train-card='${WAITLIST_TRAIN}']`);
    await expect(card.locator(".train-actions > *").first()).toHaveText("코레일 예약 대기 신청");
    // 대기 대상이 아닌 열차에는 대기 신청이 없어요.
    await expect(page.locator("[data-train-card='00105'] [data-official-waitlist]")).toHaveCount(0);
    await expect(page.locator(".waitlist-advice")).toContainText("일반실만");
    await expectCleanLayout(page, "예약 대기를 먼저 권하는 열차 목록");
  });

  test("신청하면 홈에 대기 중으로 보이고, 좌석 확보로 보이지 않아요 @layout", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);

    const card = page.locator("[data-waitlist]");
    await expect(card).toHaveAttribute("data-state", "waiting");
    await expect(card).toContainText("코레일 예약 대기 중");
    await expect(card).toContainText(trainLine(3, WAITLIST_TRAIN, "08:00→10:38"));
    await expect(page.locator(".payment-card")).toHaveCount(0);
    await expectCleanLayout(page, "예약 대기 신청 뒤 홈");
    const log = await control.korailLog();
    expect(log.filter((call) => call.event === "waitlist")).toHaveLength(1);
    // 신청은 한 번뿐이고, 예약(취소표 감시)은 시작하지 않아요.
    expect(log.some((call) => call.event === "search" || String(call.event).startsWith("reserve"))).toBe(false);
  });

  test("좌석이 배정되면 결제 카드로 넘어가 결제를 안내해요 @layout", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);
    await control.scenario({ waitlist: "assigned" });

    const payment = page.locator(".payment-card");
    for (let attempt = 0; attempt < 15 && !(await payment.isVisible()); attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    await expect(payment).toContainText("코레일 예약 대기 배정");
    await expect(payment).toContainText("3호차 7A");
    await expect(payment.locator("[data-korail-pay]")).toBeVisible();
    // 결제 카드가 보여 주니 대기 카드는 따로 남지 않아요.
    await expect(page.locator("[data-waitlist]")).toHaveCount(0);
    await expectCleanLayout(page, "예약 대기 배정 뒤 결제 카드");
  });

  test("신청한 예약 대기를 취소하면 코레일에 취소를 보내고 취소함으로 보여요 @layout", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);

    await page.locator("[data-waitlist-cancel]").click();
    await expect(page.locator(".action-sheet")).toContainText("코레일 예약 대기를 취소할까요?");
    await page.locator("[data-action='sheet-confirm']").click();

    await expect(page.locator(".toast")).toHaveText("코레일 예약 대기를 취소했어요.");
    await expect(page.locator("[data-waitlist]")).toHaveAttribute("data-state", "cancelled");
    await expectCleanLayout(page, "예약 대기 취소 뒤 홈");
    expect((await control.korailLog()).filter((call) => call.event === "waitlist_cancel")).toHaveLength(1);
  });

  test("예약 대기 확인이 실패해도 끝났다고 하지 않아요", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);
    await control.scenario({ waitlist: "unavailable" });

    const card = page.locator("[data-waitlist]");
    for (let attempt = 0; attempt < 15 && !(await card.textContent())?.includes("실패"); attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    await expect(card).toContainText("마지막 확인에 실패했어요");
    await expect(card).toHaveAttribute("data-state", "waiting");
  });

  test("예약 대기가 안 되는 매진 열차는 지금처럼 취소표 감시로 찾아요 @layout", async ({ signedIn: page, control }) => {
    await control.scenario({ list: "sold_out" });
    await searchTrains(page);
    const card = page.locator("[data-train-card='00101']");
    await expect(card.locator("[data-official-waitlist]")).toHaveCount(0);
    await expect(card.locator("[data-seat-map]")).toBeVisible();
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await expect(page.locator(".screen-confirm")).toBeVisible();
    await expect(page.locator(".waitlist-advice")).toHaveCount(0);
    await page.locator("[data-action='start-now']").click();
    const sheetConfirm = page.locator("[data-action='sheet-confirm']");
    if (await sheetConfirm.isVisible().catch(() => false)) await sheetConfirm.click();
    await expect(page.locator(".screen-home [data-action='stop-search']")).toBeVisible();
    expect((await control.korailLog()).some((call) => call.event === "waitlist")).toBe(false);
  });

  test("예약 대기가 되는 열차를 감시하려 하면 확인 화면에서 예약 대기를 권해요 @layout", async ({ signedIn: page }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, [WAITLIST_TRAIN]);
    await page.locator("[data-action='trains-next']").click();
    await expect(page.locator(".screen-confirm")).toBeVisible();
    await expect(page.locator(".waitlist-advice")).toContainText("1편은 코레일 예약 대기가 돼요");
    await expectCleanLayout(page, "감시 확인 화면의 예약 대기 권유");
  });
});
