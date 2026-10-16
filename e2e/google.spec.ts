import { execFileSync } from "node:child_process";

import { expect, openApp, signIn, test } from "./fixtures";
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

// 안드로이드 앱은 코드 없이 끝나요. 브라우저의 구글 페이지가 이 휴대폰의 앱을 dev.thsvkd.jari://google-auth?handoff=… 로 다시 열고,
// 앱은 자기 티켓과 그 값을 함께 보내야만 결과를 받아요. 기기에서는 시스템 브라우저를 실제로 열고 adb 로 그 복귀 인텐트를 보내요.
// 헤드리스(웹 빌드)는 복귀를 받을 수 없으니, 코드 없는 시작 응답의 대기 화면과 완료만 봐요.
test("안드로이드 앱은 확인 코드 없이 브라우저가 돌려준 값으로 구글 로그인을 마쳐요 @layout", async ({ app: page, user }, testInfo) => {
  const device = testInfo.project.name === "device";
  const handoff = "e2e-handoff-" + "h".repeat(32);
  let delivered = false;
  let startBody: unknown;
  const pollTickets = new Set<string>();
  await page.route("**/api/mobile/auth/google", (route) => route.fulfill({ json: { enabled: true } }));
  await page.route("**/api/mobile/auth/google/start", async (route) => {
    startBody = route.request().postDataJSON();
    await route.fulfill({ json: { ticket: "fake-app-return-proof", authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=e2e-app-return", expiresIn: 300 } });
  });
  await page.route("**/api/mobile/auth/google/ack", (route) => route.fulfill({ json: { acknowledged: true } }));
  await page.route("**/api/mobile/auth/google/cancel", (route) => route.fulfill({ json: { cancelled: true } }));
  await page.route("**/api/mobile/auth/google/poll", async (route) => {
    const body = route.request().postDataJSON() as { ticket: string; handoff?: string };
    pollTickets.add(body.ticket);
    if (device ? body.handoff !== handoff : !delivered) { await route.fulfill({ json: { status: "pending" } }); return; }
    await route.fulfill({ json: { status: "complete", auth: { token: user.token, user: { id: "ui-ignored", username: user.username }, expiresAt: "2099-01-01T00:00:00Z" } } });
  });
  await openApp(page);
  if (!device) await page.evaluate(() => { window.open = (() => ({ opener: null })) as unknown as typeof window.open; });
  await page.locator("[data-action='google-login']").click();
  const pending = page.locator(".google-pending");
  await expect(pending).toContainText("자리났다 앱 열기");
  await expect(page.locator("[data-google-code]")).toHaveCount(0);
  if (!device) {
    await expectCleanLayout(page, "구글 로그인 앱 복귀 대기");
    delivered = true;
  } else {
    // 시스템 브라우저가 앞에 올 때까지 기다린 뒤, 브라우저의 구글 페이지가 하듯 앱을 다시 열어요.
    await expect.poll(() => {
      const top = execFileSync("adb", ["shell", "dumpsys activity activities"], { encoding: "utf8" }).split("\n").find((line) => line.includes("topResumedActivity")) ?? "";
      return top && !top.includes("dev.thsvkd.jari/") ? "browser" : "";
    }, { timeout: 15_000 }).toBe("browser");
    execFileSync("adb", ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-c", "android.intent.category.BROWSABLE", "-d", `'dev.thsvkd.jari://google-auth?handoff=${handoff}'`]);
  }
  await expect.poll(() => page.evaluate(() => Boolean(document.querySelector(".screen-home"))), { timeout: 20_000 }).toBe(true);
  // 경로 처리기 안에서 실패하면 응답이 멈춰 시간 초과로만 보여서, 받은 요청은 끝에 확인해요.
  expect(startBody).toEqual(device ? { return: "app" } : {});
  expect([...pollTickets]).toEqual(["fake-app-return-proof"]);
});
