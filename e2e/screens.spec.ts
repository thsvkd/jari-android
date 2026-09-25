import type { Page } from "@playwright/test";

import { expect, openApp, test } from "./fixtures";
import { keepOnlyTrains, pickTime, searchTrains, waitForPaymentCard } from "./flows";

// 화면 단위 스크린샷 비교 @visual. 매번 바뀌는 글자(사용자 이름, 남은 시간, 상대 시각)는 가려요.
// 기준 이미지는 폰트가 OS 마다 달라 -win32/-linux 로 따로 있어요. 바꾸려면 사람이 보고 승인해요.

/**
 * 날마다·분마다 달라지는 글자를 고정값으로 바꿔요. 그대로 두면 기준 이미지가 날짜가 바뀔 때마다 깨져요.
 * 글자 수가 같은 값으로 바꿔 배치는 그대로 재요. 달력 격자는 달마다 모양이 달라 가리고, 배치는 레이아웃 규칙이 재요.
 */
async function freezeTime(page: Page): Promise<void> {
  await page.evaluate(() => {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      node.textContent = node.textContent!
        .replace(/\d{4}년 \d{1,2}월/g, "2026년 9월")
        .replace(/\d{1,2}월 \d{1,2}일/g, "9월 26일")
        .replace(/\((월|화|수|목|금|토|일)\)/g, "(토)")
        // 알림·예약 시각(오전 07:00)과 시간 칸(오전 7:00). 새 여정의 시작 시각은 지금이라 여기서 고정해요.
        .replace(/(오전|오후) \d{1,2}:\d{2}/g, "오전 07:00")
        // 가짜 코레일은 예약번호를 차례로 매겨서 앞서 돈 테스트 수에 따라 바뀌어요.
        .replace(/E2E\d{8}/g, "E2E00000001");
    }
  });
}

async function snap(page: Page, name: string): Promise<void> {
  await page.evaluate(() => window.scrollTo(0, 0));
  await freezeTime(page);
  // soft: 한 화면이 달라도 나머지 화면까지 모두 비교해 한 번에 검토해요.
  await expect.soft(page).toHaveScreenshot(`${name}.png`, {
    fullPage: true,
    animations: "disabled",
    caret: "hide",
    stylePath: "e2e/screenshot.css",
    // 비율로 두면 긴 화면에서 수만 픽셀이 바뀌어도 지나가요. 같은 기기의 렌더링은 결정적이라 몇 픽셀만 허용해요.
    maxDiffPixels: 20,
    mask: [
      page.locator(".payment-row > b"),
      page.locator(".date-grid"),
      page.locator(".date-field em"),
      page.locator(".profile-card"),
      // 앱과 서버 버전은 릴리스마다 바뀌어요.
      page.locator(".settings-row.static > em"),
      page.locator(".settings-row.static", { hasText: "앱 버전" }).locator("small"),
      page.locator(".search-last-check"),
      page.locator(".idle-badge"),
      page.locator(".notification small"),
      page.locator(".toast"),
    ],
  });
}

test.describe("화면 @visual", () => {
  test("로그인 전", async ({ app: page }) => {
    await openApp(page);
    await snap(page, "auth-gate");
    await page.locator("[data-auth-gate='guest']").click();
    await snap(page, "auth-member");
  });

  test("탭과 새 여정", async ({ signedIn: page }) => {
    await snap(page, "home-empty");
    for (const view of ["activity", "favourites", "settings"]) {
      await page.locator(`.bottom-nav [data-view='${view}']`).click();
      await snap(page, `${view}-empty`);
    }
    await page.locator(".bottom-nav [data-view='home']").click();
    await page.locator(".screen-home [data-action='new-journey']").click();
    // 기본 시각은 지금 시각이라(밤 10시가 넘으면 "마지막 열차까지"가 켜져요) 사용자처럼 고정된 값으로 골라요.
    if (await page.locator("[name=unlimited_time]").isChecked()) {
      await page.locator("label", { has: page.locator("[name=unlimited_time]") }).click();
    }
    await pickTime(page, "dep_time", "07:00");
    await pickTime(page, "max_dep_time", "09:00");
    await snap(page, "journey");
    await page.locator("[data-dp='toggle']").click();
    await snap(page, "journey-calendar");
  });

  test("열차·좌석표·조건 확인·즐겨찾기", async ({ signedIn: page }) => {
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general", "special"] });
    await snap(page, "trains-specific");
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await snap(page, "seat-dialog");
    await page.locator("[data-action='close-seat-dialog']").click();
    await page.locator("[data-action='trains-next']").click();
    await snap(page, "confirm");
    await page.locator("#favourite-name").fill("주말 부산");
    await page.locator("[data-action='save-favourite']").click();
    await page.locator(".bottom-nav [data-view='favourites']").click();
    await snap(page, "favourites");
    await page.locator(".bottom-nav [data-view='home']").click();
    await snap(page, "home-shortcuts");
    await page.locator("[data-route-chip]").first().click();
    await expect(page.locator(".action-sheet")).toBeVisible();
    // 기본 날짜는 지금 시각에 따라 오늘이나 내일이라(밤 11시가 넘으면 내일) 사용자처럼 오늘을 골라 둬요.
    await page.locator("[data-sheet-date]").first().click();
    await expect(page.locator("[data-sheet-date]").first()).toHaveAttribute("aria-pressed", "true");
    await snap(page, "date-sheet");
  });

  test("찾는 중·결제 대기·알림", async ({ signedIn: page }) => {
    await searchTrains(page);
    await snap(page, "trains-any");
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await page.locator("[data-action='start-now']").click();
    await expect(page.locator(".screen-home")).toBeVisible();
    await snap(page, "home-searching");
    await waitForPaymentCard(page);
    await snap(page, "home-payment");
    await page.locator(".bottom-nav [data-view='activity']").click();
    await snap(page, "activity-payment");
    await page.locator("[data-view='notifications']").first().click();
    // 결제 재촉 알림은 도는 시점에 따라 늘어나요. 예약 알림만 남기고 화면째 비교해요.
    await expect(page.locator(".notification", { hasText: "좌석을 잡았어요" }).first()).toBeVisible();
    await page.evaluate(() => {
      for (const item of document.querySelectorAll<HTMLElement>(".notification")) {
        if (!item.textContent?.includes("좌석을 잡았어요")) item.style.display = "none";
      }
    });
    await snap(page, "notifications-booked");
  });
});
