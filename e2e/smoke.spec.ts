import { expect, openApp, test } from "./fixtures";

test("승인된 회원이 로그인하면 코레일 계정이 연결된 홈이 보여요", async ({ signedIn: page }) => {
  await expect(page.locator(".screen-home")).toBeVisible();
  await expect(page.locator("[data-action='new-journey']")).toBeEnabled();
  await page.locator(".bottom-nav [data-view='settings']").click();
  await expect(page.locator(".screen-settings")).toContainText("연결됨");
});

test("초대 코드 없이 가입하고 비밀번호 두 칸이 다르면 막으며 가입 뒤에는 승인을 기다려요", async ({ app: page, control }) => {
  await control.reset();
  await openApp(page);
  await page.locator("[data-auth-gate='guest']").click();
  await page.locator("[data-auth-mode='register']").click();
  const form = page.locator("#auth-form");
  await form.locator("[name='username']").fill(`n${Math.random().toString(36).slice(2, 10)}`);
  await form.locator("[name='password']").fill("a long secure passphrase");
  await form.locator("[name='password_confirm']").fill("a different passphrase");
  await expect(form.locator("[name='invite']")).toHaveCount(0);
  await form.locator("[name='password_confirm']").press("Enter");
  await expect(form.locator(".form-error")).toHaveText("비밀번호가 서로 달라요.");
  await form.locator("[name='password_confirm']").fill("a long secure passphrase");
  await form.locator("[name='password_confirm']").press("Enter");
  await expect(page.locator(".screen-approval")).toContainText("승인");
  await expect(page.locator(".screen-home")).toHaveCount(0);
});
