import { expect, test } from "./fixtures";
import { expectCleanLayout } from "./layout";
import { keepOnlyTrains, searchTrains, travelDay } from "./flows";

test.describe("즐겨찾기", () => {
  test("조건을 저장하면 카드로 보이고, 토스트는 하단 버튼을 가리지 않고, 지우면 사라져요 @layout", async ({ signedIn: page }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await page.locator("#favourite-name").fill("주말 부산");
    await page.locator("[data-action='save-favourite']").click();
    await expect(page.locator(".toast")).toHaveText("즐겨찾기에 저장했어요.");

    await page.locator(".bottom-nav [data-view='favourites']").click();
    const card = page.locator(".screen-favourites .favourite-row", { hasText: "주말 부산" });
    await expect(card).toBeVisible();
    // 카드: 배경과 구분되는 테두리와 둥근 모서리가 있어요.
    const look = await card.evaluate((node) => {
      const style = getComputedStyle(node);
      return { border: style.borderTopWidth, radius: style.borderTopLeftRadius };
    });
    expect(parseFloat(look.border)).toBeGreaterThan(0);
    expect(parseFloat(look.radius)).toBeGreaterThanOrEqual(12);
    await expect(page.locator(".screen-favourites .favourite-add")).toHaveText(/새 즐겨찾기 추가/);
    // 토스트가 떠 있는 동안 재야 버튼을 가리는지 알 수 있어요.
    await expect(page.locator(".toast")).toBeVisible();
    await expectCleanLayout(page, "즐겨찾기(카드 + 토스트)");

    await page.locator(`[data-delete-favourite]`).first().click();
    const sheetConfirm = page.locator("[data-action='sheet-confirm']");
    if (await sheetConfirm.isVisible().catch(() => false)) await sheetConfirm.click();
    await expect(card).toHaveCount(0);
  });

  test("홈의 최근 구간에서 날짜 시트를 열고 달력으로 날짜를 골라요 @layout", async ({ signedIn: page }) => {
    // 홈의 바로가기 칩은 이 폰에서 조회한 구간이에요. 즐겨찾기는 칩이 아니라 아래 자주 가는 구간에만 있어요.
    await searchTrains(page);
    await page.locator("[data-action='trains-next']").click();
    await page.locator("[data-action='save-favourite']").click();
    await expect(page.locator(".toast")).toHaveText("즐겨찾기에 저장했어요.");
    await page.locator(".bottom-nav [data-view='home']").click();
    await expect(page.locator(".idle-chip")).toHaveCount(1);
    await expect(page.locator(".idle-chip")).not.toContainText("즐겨찾기");
    // 이름 없이 저장한 즐겨찾기는 이름이 곧 구간이에요. 자주 가는 구간 카드에 구간을 두 번 쓰지 않아요.
    await expect(page.locator(".route-list .favourite-main b")).toHaveText("서울 → 부산");
    await expect(page.locator(".route-list .favourite-main small")).not.toContainText("→");
    await expectCleanLayout(page, "홈(이름 없는 즐겨찾기)");
    const chip = page.locator("[data-route-chip]").first();
    await expect(chip).toBeVisible();
    await chip.click();
    await expect(page.locator(".action-sheet")).toBeVisible();
    await expectCleanLayout(page, "날짜 시트");
    const day = travelDay(5).iso;
    const target = page.locator(`.action-sheet [data-dp-day='${day}']:not([disabled])`);
    for (let month = 0; month < 13 && !(await target.isVisible()); month++) {
      await page.locator(".action-sheet [data-dp='next']").click();
    }
    await target.click();
    await expect(page.locator("#sheet-date")).toHaveValue(day);
  });
});

test.describe("설정", () => {
  test("테마를 바꾸면 화면 전체가 따라 바뀌고, 로그아웃은 맨 아래에서 로그인 화면으로 돌려보내요", async ({ signedIn: page }) => {
    await page.locator(".bottom-nav [data-view='settings']").click();
    const before = await page.locator("html").getAttribute("data-theme");
    await page.locator("[data-action='theme']").click();
    await expect(page.locator("html")).not.toHaveAttribute("data-theme", before!);
    await expectCleanLayout(page, "설정(테마 바꾼 뒤)");

    const buttons = page.locator(".screen-settings button");
    await expect(buttons.last()).toHaveAttribute("data-action", "app-logout");
    await buttons.last().click();
    const sheetConfirm = page.locator("[data-action='sheet-confirm']");
    if (await sheetConfirm.isVisible().catch(() => false)) await sheetConfirm.click();
    await expect(page.locator(".auth-shell")).toBeVisible();
  });
});
