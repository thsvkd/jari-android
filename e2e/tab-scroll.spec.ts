import { expect, nextPoll, openApp, test } from "./fixtures";
import { expectCleanLayout } from "./layout";

test("빈 탭은 여백 때문에 스크롤되지 않아요 @layout", async ({ signedIn: page }) => {
  await expect(page.locator("[data-search-status]")).toBeVisible();
  for (const view of ["home", "activity", "favourites"]) {
    await page.locator(`.bottom-nav [data-view='${view}']`).click();
    await expectCleanLayout(page, `빈 ${view} 탭`, { noPageScroll: true });
  }
});

test("체험 배너가 있어도 빈 활동 탭은 스크롤되지 않아요 @layout", async ({ app: page }) => {
  await openApp(page);
  await page.locator(".gate-card").nth(2).click();
  await page.locator(".bottom-nav [data-view='activity']").click();
  // 체험 API의 샘플 검색만 끝내 빈 탭을 만들어요.
  await page.locator("[data-action='cancel-search']").click();
  await page.locator("[data-action='sheet-confirm']").click();
  await expect(page.locator("[data-trial-banner]")).toBeVisible();
  await expect(page.getByText("찾고 있는 자리가 없어요", { exact: true })).toBeVisible();
  await expectCleanLayout(page, "체험 중 빈 활동 탭", { noPageScroll: true });
});

test("오프라인 배너가 있어도 빈 활동 탭은 스크롤되지 않아요 @layout", async ({ signedIn: page, context }, testInfo) => {
  test.skip(testInfo.project.name === "device", "기기의 네트워크는 끊지 않고 헤드리스에서 검사해요");
  await expect(page.locator("[data-search-status]")).toBeVisible();
  try {
    await context.setOffline(true);
    await nextPoll(page);
    await expect(page.locator(".offline-banner")).toBeVisible();
    await page.locator(".bottom-nav [data-view='activity']").click();
    await expectCleanLayout(page, "오프라인 빈 활동 탭", { noPageScroll: true });
  } finally {
    await context.setOffline(false);
  }
});
