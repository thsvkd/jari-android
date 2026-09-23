import { expect, nextPoll, test } from "./fixtures";
import { keepOnlyTrains, searchTrains, startSearching, trainLine, waitForPaymentCard } from "./flows";

// 코레일만 가짜예요. 로그인·조회·예약 워커·결제 감시·알림은 운영 코드가 그대로 돌아요.

test.describe("빈자리를 찾아 예약하기", () => {
  test("워커가 좌석을 잡으면 홈에는 결제 카드만, 한 줄 열차 정보와 줄어드는 남은 시간이 보여요", async ({ signedIn: page, control }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    await expect(page.locator(".screen-home")).toBeVisible();

    await waitForPaymentCard(page);

    const card = page.locator(".screen-home .payment-card");
    await expect(card.locator(".payment-row strong")).toHaveText(trainLine());
    await expect(page.locator("[data-search-status]")).toHaveCount(0);
    const left = card.locator("[data-deadline]");
    await expect(left).toHaveText(/^\d{1,2}:\d{2} 남음$/);
    const before = await left.textContent();
    await page.clock.runFor(3_000);
    await expect(left).not.toHaveText(before!);
    expect((await control.korailLog()).filter((call) => call.event === "reserve_train")).toHaveLength(1);
  });

  test("예약 알림은 앱 문구로, 줄바꿈이 살아서 보여요", async ({ signedIn: page }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    await waitForPaymentCard(page);

    await page.locator("[data-view='notifications']").first().click();
    const notice = page.locator(".notification", { hasText: "좌석을 잡았어요" }).first();
    await expect(notice).toBeVisible();
    await expect(notice.locator(".notification-icon")).toHaveText("예약");
    const text = await notice.locator("b").innerText();
    expect(text.split("\n")[0]).toBe("🎉 좌석을 잡았어요");
    expect(text.split("\n")[1]).toBe(trainLine());
    for (const leftover of ["/notify_off", "/tickets", "봇", "===", "출발역"]) expect(text).not.toContain(leftover);
  });

  test("코레일에서 결제하면 결제 카드가 사라져요", async ({ signedIn: page, control }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    await waitForPaymentCard(page);

    await control.scenario({ outcome: "PAID" });
    for (let attempt = 0; attempt < 10 && (await page.locator(".payment-card").isVisible()); attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    await expect(page.locator(".payment-card")).toHaveCount(0);
  });

  test("결제 전 예약을 취소하면 코레일에 좌석을 돌려줘요", async ({ signedIn: page, control }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    await waitForPaymentCard(page);

    await page.locator(".bottom-nav [data-view='activity']").click();
    await page.locator("[data-action='cancel-pending']").click();
    await page.locator("[data-action='sheet-confirm']").click();

    await expect(page.locator(".payment-card")).toHaveCount(0);
    await expect(page.locator(".toast")).toHaveText("예약을 취소했어요.");
    expect((await control.korailLog()).filter((call) => call.event === "cancel")).toHaveLength(1);
  });

  test("코레일 조회가 실패하면 매진과 다르게, 실패라고 보여요", async ({ signedIn: page, control }) => {
    await control.scenario({ search: "unavailable" });
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    const status = page.locator("[data-search-status]");
    for (let attempt = 0; attempt < 20 && (await status.getAttribute("data-state")) !== "error"; attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    // 멈춤(stale)이나 미확인이 아니라, 조회가 실패했다고 말해야 해요.
    await expect(status).toHaveAttribute("data-state", "error");
    await expect(status).toContainText("철도 조회를 완료하지 못했어요");
    await expect(status).not.toContainText("찾는 중");
    const searches = (await control.korailLog()).filter((call) => call.event === "search");
    expect(searches.length).toBeGreaterThan(0);
    expect(new Set(searches.map((call) => call.mode))).toEqual(new Set(["unavailable"]));
  });

  test("좌석표에서 고른 좌석을 바로 예약하면 호차·좌석과 함께 결제 카드가 보여요", async ({ signedIn: page, control }) => {
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general"] });
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
    const seat = page.locator(".seat-cell.available:not([disabled])").first();
    await expect(seat).toBeVisible();
    await seat.click();
    await page.locator("[data-action='confirm-seat-dialog']").click();
    // 실제 예약이라 열차와 호차·좌석을 보여 주고 한 번 더 물어요.
    await expect(page.locator(".action-sheet")).toContainText("1호차");
    await page.locator("[data-action='sheet-confirm']").click();

    await expect(page.locator(".payment-card").first()).toBeVisible();
    await expect(page.locator(".payment-card .payment-row strong").first()).toHaveText(trainLine());
    await expect(page.locator(".payment-card .payment-row small").first()).toContainText("1호차");
    expect((await control.korailLog()).filter((call) => call.event === "reserve_designated")).toHaveLength(1);
  });
});
