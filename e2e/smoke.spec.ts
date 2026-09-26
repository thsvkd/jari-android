import { expect, openApp, test } from "./fixtures";

test("초대 회원이 로그인하면 코레일 계정이 연결된 홈이 보여요", async ({ signedIn: page }) => {
  await expect(page.locator(".screen-home")).toBeVisible();
  await expect(page.locator("[data-action='new-journey']")).toBeEnabled();
  await page.locator(".bottom-nav [data-view='settings']").click();
  await expect(page.locator(".screen-settings")).toContainText("연결됨");
});

test("초대 코드는 띄어 쓰거나 대문자로 적어도 되고, 비밀번호 두 칸이 달라야 막혀요", async ({ app: page, control }) => {
  const invite = await control.newInvite();
  await openApp(page);
  await page.locator("[data-auth-gate='guest']").click();
  await page.locator("[data-auth-mode='register']").click();
  const form = page.locator("#auth-form");
  await form.locator("[name='username']").fill(`n${Math.random().toString(36).slice(2, 10)}`);
  await form.locator("[name='password']").fill("a long secure passphrase");
  await form.locator("[name='password_confirm']").fill("a different passphrase");
  await form.locator("[name='invite']").fill(invite.split("-").join(" ").toUpperCase());
  await form.locator("[name='invite']").press("Enter");
  await expect(form.locator(".form-error")).toHaveText("비밀번호가 서로 달라요.");
  await form.locator("[name='password_confirm']").fill("a long secure passphrase");
  await form.locator("[name='invite']").press("Enter");
  await expect(page.locator(".screen-home")).toBeVisible();
});
