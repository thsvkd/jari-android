import { execFileSync } from "node:child_process";

import { expect, test } from "./fixtures";

// 기기(에뮬레이터)에서만 돌아요. playwright.config.ts 가 헤드리스 프로젝트에서는 이 파일을 빼요.
// adb 는 verify.mjs 가 고른 기기(ANDROID_SERIAL)로 보내요.
function pressAndroidBack(): void {
  execFileSync("adb", ["shell", "input", "keyevent", "KEYCODE_BACK"]);
}

function foregroundActivity(): string {
  const activities = execFileSync("adb", ["shell", "dumpsys activity activities"], { encoding: "utf8" });
  return activities.split("\n").find((line) => line.includes("topResumedActivity")) ?? "";
}

test.describe("Android 기기", () => {
  // targetSdk 36(Android 16)에서는 예측 뒤로가기가 켜져 Activity.onBackPressed 가 불리지 않아요.
  // 앱의 뒤로가기(@capacitor/app 의 backButton)는 OnBackPressedDispatcher 로 받으니 그대로 와야 해요.
  test("뒤로가기는 떠 있는 시트를 먼저 닫고, 그다음 앞 화면으로 돌아가요", async ({ signedIn: page }) => {
    await page.locator(".bottom-nav [data-view='settings']").click();
    await page.locator("[data-view='rail-account']").click();
    await expect(page.locator(".screen-rail-account")).toBeVisible();
    await page.locator("[data-action='rail-logout']").click();
    await expect(page.locator(".action-sheet")).toBeVisible();

    pressAndroidBack();
    await expect(page.locator(".action-sheet")).toHaveCount(0);
    await expect(page.locator(".screen-rail-account")).toBeVisible();

    pressAndroidBack();
    await expect(page.locator(".screen-settings")).toBeVisible();
  });

  // Play 는 앱 안에서 개인정보처리방침에 닿아야 해요. 링크는 앱 WebView 가 아니라 시스템 브라우저로 열려야 해요.
  test("개인정보처리방침 링크는 앱을 떠나 브라우저로 열고, 앱은 설정 화면 그대로예요", async ({ signedIn: page }) => {
    await page.locator(".bottom-nav [data-view='settings']").click();
    // 이동은 Capacitor 가 가로채 브라우저로 넘기니 WebView 에는 끝나는 이동이 없어요. 기다리지 않아요.
    await page.locator(".policy-links a[href='https://jari.thsvkd.dev/privacy']").click({ noWaitAfter: true });
    await expect.poll(foregroundActivity, { timeout: 15_000 }).not.toContain("dev.thsvkd.jari/");
    expect(foregroundActivity()).not.toBe("");
    expect(new URL(page.url()).host).toBe("localhost");

    // 사용자처럼 뒤로가기로 앱에 돌아와요. 브라우저가 첫 실행 화면 등으로 뒤로가기를 먹으면 앱을 앞으로 불러와요.
    pressAndroidBack();
    const back = await expect
      .poll(foregroundActivity, { timeout: 10_000 })
      .toContain("dev.thsvkd.jari/")
      .then(() => true, () => false);
    if (!back) {
      execFileSync("adb", ["shell", "am", "start", "-n", "dev.thsvkd.jari/com.jari.app.MainActivity"]);
      await expect.poll(foregroundActivity, { timeout: 15_000 }).toContain("dev.thsvkd.jari/");
    }
    // WebView 가 시작만 하고 넘겨준 이동을 Playwright 는 끝나지 않은 이동으로 봐서 locator 가 기다려요. 문서를 직접 읽어요.
    await expect
      .poll(() => page.evaluate(() => Boolean(document.querySelector(".screen-settings .policy-links")) && location.host), { timeout: 10_000 })
      .toBe("localhost");
  });
});
