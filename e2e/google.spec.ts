import { expect, signIn, test } from "./fixtures";
import { expectCleanLayout } from "./layout";

// UI 계약 검증: Google 자체는 호출하지 않고, API 성공·취소 응답을 경계에서 대체해요.
for (const legacyHint of [undefined, true, false]) {
test(`설정에서 로그인 세션으로 비밀번호 없이 구글을 연결해요 (이전 상태 ${legacyHint}) @layout`, async ({ app: page, user }, testInfo) => {
  test.skip(testInfo.project.name === "device", "Google API·외부 브라우저 대체는 헤드리스에서 검사해요");
  let failLink = true;
  let linked = false;
  let purpose: "login" | "link" = "link";
  let waiting = true;
  let failCancel = true;
  await page.route("**/api/mobile/auth/google", (route) => route.fulfill({ json: { enabled: true } }));
  await page.route("**/api/mobile/account/google", (route) => route.fulfill({ json: { enabled: true, linked, reauthenticationRequired: legacyHint } }));
  await page.route("**/api/mobile/account/google/link", async (route) => {
    expect(route.request().postDataJSON()).toEqual({});
    if (failLink) {
      failLink = false;
      await route.fulfill({ status: 503, json: { error: "구글 연결을 시작하지 못했어요. 다시 시도해 주세요." } });
      return;
    }
    purpose = "link";
    await route.fulfill({ json: { ticket: "fake-link-proof", authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=fake", expiresIn: 300, verificationCode: "ABCD-EFGH" } });
  });
  await page.route("**/api/mobile/auth/google/start", async (route) => {
    purpose = "login";
    await route.fulfill({ json: { ticket: "fake-login-proof", authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=fake", expiresIn: 300, verificationCode: "ABCD-EFGH" } });
  });
  await page.route("**/api/mobile/auth/google/ack", (route) => route.fulfill({ json: { acknowledged: true } }));
  await page.route("**/api/mobile/auth/google/cancel", async (route) => {
    if (failCancel) {
      failCancel = false;
      await route.fulfill({ status: 503, json: { error: "temporarily unavailable" } });
    } else await route.fulfill({ json: { cancelled: true } });
  });
  await page.route("**/api/mobile/auth/google/poll", async (route) => {
    if (waiting) { await route.fulfill({ json: { status: "pending" } }); return; }
    if (purpose === "link") {
      linked = true;
      await route.fulfill({ json: { status: "complete", linked: true } });
    } else {
      await route.fulfill({ json: { status: "complete", auth: { token: user.token, user: { id: "ui-ignored", username: user.username }, expiresAt: "2099-01-01T00:00:00Z" } } });
    }
  });
  await signIn(page, user);
  // Native Google 브라우저를 열지 않도록 Capacitor bridge 대신 JS 호출을 테스트해요.
  // 기기 프로젝트는 이 브라우저 대체를 쓸 수 없어 별도 네이티브 빌드·수동 smoke 범위예요.
  await page.evaluate(() => { window.open = (() => ({ opener: null })) as unknown as typeof window.open; });
  await page.locator(".bottom-nav [data-view='settings']").click();
  const googleMark = page.locator("[data-action='google-link'] .google-brand-icon");
  await expect(googleMark).toBeVisible();
  await expect.poll(() => googleMark.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
  await expect(googleMark).toHaveAttribute("alt", "");
  await expect(googleMark).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expectCleanLayout(page, "구글 연결 메뉴와 공식 로고");
  await page.locator("[data-action='google-link']").click();
  await expect(page.getByText("구글 연결을 시작하지 못했어요. 다시 시도해 주세요.", { exact: true })).toBeVisible();
  await expect(page.locator("#sheet-password")).toHaveCount(0);
  await expectCleanLayout(page, "구글 연결 시작 실패와 재시도");
  await page.locator("[data-action='google-link']").click();
  await expect(page.locator("#sheet-password")).toHaveCount(0);
  await expect(page.locator("[data-action='google-cancel']")).toBeVisible();
  await expect(page.locator("[data-google-code]")).toHaveText("ABCD-EFGH");
  await expectCleanLayout(page, "구글 인증 대기");
  waiting = false;
  await expect(page.getByText("구글 계정 연결됨", { exact: true })).toBeVisible();
  await expect(page.locator("[data-action='google-unlink'] .google-brand-icon")).toBeVisible();
  await expectCleanLayout(page, "구글 연결 완료");
  await page.locator("[data-action='app-logout']").click();
  const confirm = page.locator("[data-action='sheet-confirm']");
  if (await confirm.isVisible()) await confirm.click();
  await expect(page.locator("[data-action='google-login']")).toBeVisible();
  await expectCleanLayout(page, "구글 로그인 선택");
  waiting = true;
  await page.locator("[data-action='google-login']").click();
  await expect(page.locator("[data-action='google-cancel']")).toBeVisible();
  await page.locator("[data-action='google-cancel']").click();
  await expect(page.locator(".google-pending")).toContainText("취소를 확인하지 못했어요");
  await expectCleanLayout(page, "구글 로그인 취소 재시도");
  await page.locator("[data-action='google-cancel']").click();
  await expect(page.locator("[data-action='google-login']")).toBeEnabled();
  waiting = false;
  await page.locator("[data-action='google-login']").click();
  await expect(page.locator(".screen-home")).toBeVisible();
});

}
