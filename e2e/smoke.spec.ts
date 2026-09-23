import { expect, test } from "./fixtures";

test("초대 회원이 로그인하면 코레일 계정이 연결된 홈이 보여요", async ({ signedIn: page }) => {
  await expect(page.locator(".screen-home")).toBeVisible();
  await expect(page.locator("[data-action='new-journey']")).toBeEnabled();
  await page.locator(".bottom-nav [data-view='settings']").click();
  await expect(page.locator(".screen-settings")).toContainText("연결됨");
});
