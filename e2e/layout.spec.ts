import { expect, openApp, test } from "./fixtures";
import { expectCleanLayout } from "./layout";
import { keepOnlyTrains, searchTrains, waitForPaymentCard } from "./flows";

// 사용자가 거치는 모든 화면·상태에서 레이아웃 규칙을 재요. phone-dark 프로젝트가 같은 검사를 어두운 테마로 한 번 더 해요.

test.describe("레이아웃 @layout", () => {
  test("로그인 전 화면", async ({ app: page }) => {
    await openApp(page);
    await expectCleanLayout(page, "로그인 방법 고르기");
    await page.locator("[data-auth-gate='guest']").click();
    await expectCleanLayout(page, "초대 회원 로그인");
  });

  test("홈·탭 화면(빈 상태)", async ({ signedIn: page }) => {
    await expectCleanLayout(page, "홈");
    for (const view of ["activity", "favourites", "settings"]) {
      await page.locator(`.bottom-nav [data-view='${view}']`).click();
      await expect(page.locator(`.screen-${view}`)).toBeVisible();
      await expectCleanLayout(page, view);
    }
    await page.locator("[data-view='notifications']").first().click();
    await expectCleanLayout(page, "알림");
  });

  test("새 여정 → 열차 → 좌석표 → 조건 확인", async ({ signedIn: page }) => {
    await page.locator(".screen-home [data-action='new-journey']").click();
    await expectCleanLayout(page, "새 여정");
    await page.locator("[data-dp='toggle']").click();
    await expect(page.locator("[data-dp-day]").first()).toBeVisible();
    await expectCleanLayout(page, "새 여정(달력 펼침)");
    for (const name of ["dep_time", "max_dep_time"]) {
      const toggle = page.locator("[data-time-picker]", { has: page.locator(`[name=${name}]`) }).locator("[data-tp='toggle']");
      // 밤 10시가 넘어 돌면 기본값이 "마지막 열차까지"라 끝 시각 칸이 꺼져 있어요.
      if (await toggle.isDisabled()) continue;
      await toggle.click();
      await expect(page.locator(".time-panel [data-tp-minute]").first()).toBeVisible();
      await expectCleanLayout(page, `새 여정(${name} 시간 펼침)`);
    }
    await page.locator(".bottom-nav [data-view='home']").click();
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general", "special"] });
    await expectCleanLayout(page, "열차 목록(좌석 지정)");
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await expectCleanLayout(page, "좌석표");
  });

  test("찾는 중·결제 대기", async ({ signedIn: page }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await expectCleanLayout(page, "조건 확인");
    await page.locator("[data-action='schedule-toggle']").click();
    await page.locator("[data-time-picker='schedule-time'] [data-tp='toggle']").click();
    await expect(page.locator(".time-panel [data-tp-hour='0']")).toBeVisible();
    await expectCleanLayout(page, "조건 확인(찾기 시작 시각 펼침)");
    await page.locator("[data-date-picker='schedule-date'] [data-dp='toggle']").click();
    await expect(page.locator(".schedule-panel [data-dp-day]").first()).toBeVisible();
    await expectCleanLayout(page, "조건 확인(찾기 시작 날짜 펼침)");
    await page.locator("[data-action='start-now']").click();
    await expect(page.locator(".screen-home")).toBeVisible();
    await expectCleanLayout(page, "홈(찾는 중)");
    await waitForPaymentCard(page);
    await expectCleanLayout(page, "홈(결제 대기)");
    await page.locator(".bottom-nav [data-view='activity']").click();
    await expectCleanLayout(page, "내 예약(결제 대기)");
  });
});

test("좌석표와 시트는 Escape 로 닫히고, 화면은 그대로예요", async ({ signedIn: page }) => {
  await searchTrains(page, { seatMode: "specific", seatClasses: ["general"] });
  await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
  await expect(page.locator(".seat-dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".seat-dialog")).toHaveCount(0);
  await expect(page.locator(".screen-trains")).toBeVisible();
});
