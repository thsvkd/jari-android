import { expect, nextPoll, test } from "./fixtures";
import { keepOnlyTrains, pickDate, pickTime, searchTrains, startSearching, trainLine, travelDay, waitForPaymentCard } from "./flows";
import { expectCleanLayout } from "./layout";

// 코레일만 가짜예요. 로그인·조회·예약 워커·결제 감시·알림은 운영 코드가 그대로 돌아요.

test.describe("빈자리를 찾아 예약하기", () => {
  for (const { name, trainNo, seatClasses } of [
    { name: "좌석 예약", trainNo: "00101", seatClasses: ["general"] as const },
    { name: "취소표 대기", trainNo: "00103", seatClasses: undefined },
    { name: "일반실·특실 지정", trainNo: "00103", seatClasses: ["general", "special"] as const },
  ]) {
    test(`${name}: 좌석 지정 버튼 밖 카드 전체를 눌러 선택·해제해요 @layout`, async ({ signedIn: page }) => {
      await searchTrains(page, seatClasses ? { seatMode: "specific", seatClasses: [...seatClasses] } : {});
      const card = page.locator(".train-card", { has: page.locator(`[data-train-toggle='${trainNo}']`) });
      const toggle = card.locator("[data-train-toggle]");
      await expect(toggle).toHaveAttribute("aria-pressed", "false");

      for (const area of ["padding", "badge", "actions"]) {
        const clickArea = async () => {
          if (area === "badge") {
            await card.locator(".seat-badge").click();
          } else {
            const target = area === "padding" ? card : card.locator(".train-actions");
            await target.scrollIntoViewIfNeeded();
            const box = (await target.boundingBox())!;
            // 아래쪽 카드 패딩, 한 버튼 옆 빈 칸 또는 두 버튼 사이 틈을 직접 눌러요.
            await target.click({ position: area === "padding"
              ? { x: 5, y: box.height - 5 }
              : { x: seatClasses?.length === 2 ? box.width / 2 : box.width * 0.75, y: 24 } });
          }
        };
        await clickArea();
        await expect(toggle).toHaveAttribute("aria-pressed", "true");
        await expect(card).toContainText("좌석 무관 선택");
        await expect(page.locator(".seat-dialog")).toHaveCount(0);
        await expectCleanLayout(page, `${name} ${area} 선택`);
        await clickArea();
        await expect(toggle).toHaveAttribute("aria-pressed", "false");
        await expect(card).not.toHaveClass(/selected/);
        await expectCleanLayout(page, `${name} ${area} 해제`);
      }

      // 버튼 안쪽의 글자를 눌러도 카드 선택으로 새지 않고 좌석표만 열어요.
      await card.locator("[data-seat-map][data-seat-class='general'] span").first().click();
      await expect(page.locator(".seat-cell").first()).toBeVisible();
      await page.locator("[data-action='close-seat-dialog']").click();
      await expect(toggle).toHaveAttribute("aria-pressed", "false");
      await expect(card).not.toHaveClass(/selected/);
    });
  }

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
    // 종류는 그림과 색으로, 제목에서는 서버가 붙인 그림 글자를 뗐어요.
    await expect(notice.locator(".notification-icon")).toHaveAttribute("aria-label", "예약");
    await expect(notice.locator(".notification-head b")).toHaveText("좌석을 잡았어요");
    await expect(notice.locator(".notification-detail")).toHaveText(trainLine());
    const text = await notice.innerText();
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

  test("좌석을 잡은 뒤 다른 열차를 감시해도 홈에 둘 다 보이고, 예약 취소도 돼요 @layout", async ({ signedIn: page, control }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    await waitForPaymentCard(page);

    await control.scenario({ search: "sold_out" });
    await page.locator(".screen-home [data-action='new-journey']").click();
    await expect(page.locator(".screen-journey")).toBeVisible();
    await pickDate(page, travelDay(3).iso);
    await pickTime(page, "dep_time", "07:00");
    await page.locator("[name=unlimited_time]").check();
    await page.getByRole("button", { name: /열차 조회하기/ }).click();
    await expect(page.locator(".screen-trains [data-train-toggle]").first()).toBeVisible();
    await keepOnlyTrains(page, ["00103"]);
    await startSearching(page);
    await expect(page.locator(".screen-home")).toBeVisible();

    const status = page.locator("[data-search-status]");
    for (let attempt = 0; attempt < 10 && (await status.count()) === 0; attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    // 결제 카드는 예약을, 찾는 중 카드는 지켜보는 열차를 말해요. 한쪽이 다른 쪽을 가리면 안 돼요.
    await expect(page.locator(".screen-home .payment-card")).toContainText(trainLine());
    await expect(status).toContainText("찾는 중");
    await expect(status.locator(".watched-trains")).toContainText(/\d{2}:\d{2}→\d{2}:\d{2}/);
    await expectCleanLayout(page, "결제 대기 + 다른 열차 감시 홈");

    await page.locator(".bottom-nav [data-view='activity']").click();
    await page.locator("[data-action='cancel-pending']").click();
    await page.locator("[data-action='sheet-confirm']").click();

    await expect(page.locator(".toast")).toHaveText("예약을 취소했어요.");
    await expect(page.locator(".payment-card")).toHaveCount(0);
    await expect(page.locator("[data-action='cancel-search']")).toBeVisible();
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

  test("좌석이 남은 열차에서 매진 좌석을 누르면 취소표 대기로 바뀌고 빈 좌석 선택은 풀려요", async ({ signedIn: page }) => {
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general"] });
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
    await page.locator(".seat-cell.available:not([disabled])").first().click();
    await expect(page.locator("[data-action='confirm-seat-dialog']")).toHaveText("1/1석 선택 · 예약하기");

    await page.locator(".seat-cell.occupied").first().click();
    await expect(page.locator(".seat-mode [aria-checked='true']")).toHaveText("취소표 대기");
    await expect(page.locator(".seat-mode-notice")).toHaveText("빈 좌석 1석 선택을 풀었어요");
    await expect(page.locator(".seat-cell.selected")).toHaveCount(1);
    await expect(page.locator(".seat-cell.selected")).toHaveClass(/occupied/);
    await expectCleanLayout(page, "seat-dialog-mode-switch");
    await page.locator("[data-action='confirm-seat-dialog']").click();

    await expect(page.locator(".seat-dialog")).toHaveCount(0);
    await expect(page.locator(".train-card", { has: page.locator("[data-train-toggle='00101']") })).toContainText("1석 지정");
  });

  test("가족석은 \"제외\" 스위치 하나로 고르고, 켜면 4인 동반석 줄을 빼요 @layout", async ({ signedIn: page }) => {
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general"], passengerCount: 4 });
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await expect(page.locator("[data-seat-filter='family:only']")).toHaveCount(0);
    const exclude = page.locator("[data-seat-filter='family:exclude']");
    await expect(exclude).toHaveAttribute("role", "switch");
    await expect(exclude).toHaveAttribute("aria-checked", "false");
    // 창가·복도 칩과 같은 줄의 칩이 아니라 조건 칸 밖의 한 줄이에요.
    expect(await exclude.evaluate((node) => node.closest(".seat-filter") === null && !node.classList.contains("seat-chip"))).toBe(true);
    await exclude.click();
    await expect(exclude).toHaveAttribute("aria-checked", "true");
    await expect(page.locator(".seat-row.excluded")).toHaveCount(2);
    const picked = await page.locator(".seat-cell.selected small").allTextContents();
    expect(picked).toHaveLength(4);
    expect(picked).not.toContain("가족");
    await expectCleanLayout(page, "좌석표(가족석 제외)");
  });

  test("거의 매진된 열차도 편성의 모든 호차가 탭에 보이고 매진 호차의 좌석을 취소표 대기로 고를 수 있어요 @layout", async ({ signedIn: page, control }) => {
    // 코레일은 잔여석이 있는 호차만 목록에 줘서, 예전에는 3호차 한 개만 탭에 떴어요.
    await control.scenario({ sold_out_cars: [1, 2] });
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general"] });
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();

    await expect(page.locator(".car-tab")).toHaveCount(3);
    await expect(page.locator(".car-tab small")).toHaveText(["매진", "매진", "40석 가능"]);
    // 시트는 좌석이 남은 호차(3호차)에서 열려요.
    await expect(page.locator("[data-seat-car='3']")).toHaveClass(/selected/);
    await expect(page.locator(".seat-cell.available:not([disabled])").first()).toBeVisible();

    await page.locator("[data-seat-car='1']").click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await expect(page.locator(".seat-cell.available")).toHaveCount(0);
    await page.locator(".seat-cell.occupied").first().click();
    await expect(page.locator(".seat-mode [aria-checked='true']")).toHaveText("취소표 대기");
    await expect(page.locator("[data-seat-car='1'] em")).toHaveText("1");
    await expectCleanLayout(page, "seat-dialog-sold-out-car");
    await page.locator("[data-action='confirm-seat-dialog']").click();

    await expect(page.locator(".seat-dialog")).toHaveCount(0);
    await expect(page.locator(".train-card", { has: page.locator("[data-train-toggle='00101']") })).toContainText("1석 지정");
  });
});
