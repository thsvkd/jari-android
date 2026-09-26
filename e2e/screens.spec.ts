import type { Page } from "@playwright/test";

import { expect, openApp, test } from "./fixtures";
import { keepOnlyTrains, pickTime, searchTrains, waitForPaymentCard } from "./flows";
import { expectCleanLayout } from "./layout";

// 화면마다 실제 렌더링을 재요(겹침·넘침·잘린 글자·터치 크기·여백선·하단 메뉴 간격). 이미지 비교는 하지 않아요.
async function measure(page: Page, name: string): Promise<void> {
  await expectCleanLayout(page, name);
}

test.describe("화면 @layout", () => {
  test("로그인 전", async ({ app: page }) => {
    await openApp(page);
    await measure(page, "auth-gate");
    await page.locator("[data-auth-gate='guest']").click();
    await measure(page, "auth-member");
    await page.locator("[data-auth-mode='register']").click();
    await measure(page, "auth-register");
  });

  test("탭과 새 여정", async ({ signedIn: page }) => {
    await measure(page, "home-empty");
    for (const view of ["activity", "favourites", "settings"]) {
      await page.locator(`.bottom-nav [data-view='${view}']`).click();
      await measure(page, `${view}-empty`);
    }
    await page.locator(".bottom-nav [data-view='home']").click();
    await page.locator(".screen-home [data-action='new-journey']").click();
    // 기본 시각은 지금 시각이라(밤 10시가 넘으면 "마지막 열차까지"가 켜져요) 사용자처럼 고정된 값으로 골라요.
    if (await page.locator("[name=unlimited_time]").isChecked()) {
      await page.locator("label", { has: page.locator("[name=unlimited_time]") }).click();
    }
    await pickTime(page, "dep_time", "07:00");
    await pickTime(page, "max_dep_time", "09:00");
    await measure(page, "journey");
    await page.locator("[data-dp='toggle']").click();
    await measure(page, "journey-calendar");
  });

  test("열차·좌석표·조건 확인·즐겨찾기", async ({ signedIn: page }) => {
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general", "special"] });
    await measure(page, "trains-specific");
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await measure(page, "seat-dialog");
    await page.locator("[data-action='close-seat-dialog']").click();
    await page.locator("[data-action='trains-next']").click();
    await measure(page, "confirm");
    await page.locator("#favourite-name").fill("주말 부산");
    await page.locator("[data-action='save-favourite']").click();
    await page.locator(".bottom-nav [data-view='favourites']").click();
    await measure(page, "favourites");
    await page.locator(".bottom-nav [data-view='home']").click();
    await measure(page, "home-shortcuts");
    await page.locator("[data-route-chip]").first().click();
    await expect(page.locator(".action-sheet")).toBeVisible();
    // 기본 날짜는 지금 시각에 따라 오늘이나 내일이라(밤 11시가 넘으면 내일) 사용자처럼 오늘을 골라 둬요.
    await page.locator("[data-sheet-date]").first().click();
    await expect(page.locator("[data-sheet-date]").first()).toHaveAttribute("aria-pressed", "true");
    await measure(page, "date-sheet");
  });

  test("찾는 중·결제 대기·알림", async ({ signedIn: page }) => {
    await searchTrains(page);
    await measure(page, "trains-any");
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await page.locator("[data-action='start-now']").click();
    await expect(page.locator(".screen-home")).toBeVisible();
    await measure(page, "home-searching");
    await waitForPaymentCard(page);
    await measure(page, "home-payment");
    await page.locator(".bottom-nav [data-view='activity']").click();
    await measure(page, "activity-payment");
    await page.locator("[data-view='notifications']").first().click();
    // 결제 재촉 알림은 도는 시점에 따라 늘어나요. 예약 알림만 남기고 화면째 비교해요.
    await expect(page.locator(".notification", { hasText: "좌석을 잡았어요" }).first()).toBeVisible();
    await page.evaluate(() => {
      for (const item of document.querySelectorAll<HTMLElement>(".notification")) {
        if (!item.textContent?.includes("좌석을 잡았어요")) item.style.display = "none";
      }
    });
    await measure(page, "notifications-booked");
  });
});
