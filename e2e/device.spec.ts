import { execFileSync } from "node:child_process";

import { expect, test } from "./fixtures";

// 기기(에뮬레이터)에서만 돌아요. playwright.config.ts 가 헤드리스 프로젝트에서는 이 파일을 빼요.
// adb 는 verify.mjs 가 고른 기기(ANDROID_SERIAL)로 보내요.
function pressAndroidBack(): void {
  execFileSync("adb", ["shell", "input", "keyevent", "KEYCODE_BACK"]);
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
});
