import { execFileSync } from "node:child_process";

import { expect, test } from "./fixtures";
import { keepOnlyTrains, searchTrains, startSearching, waitForPaymentCard } from "./flows";

// 기기(에뮬레이터)에서만 돌아요. playwright.config.ts 가 헤드리스 프로젝트에서는 이 파일을 빼요.
// adb 는 verify.mjs 가 고른 기기(ANDROID_SERIAL)로 보내요.
function pressAndroidBack(): void {
  execFileSync("adb", ["shell", "input", "keyevent", "KEYCODE_BACK"]);
}

function foregroundActivity(): string {
  const activities = execFileSync("adb", ["shell", "dumpsys activity activities"], { encoding: "utf8" });
  return activities.split("\n").find((line) => line.includes("topResumedActivity")) ?? "";
}

/** 다른 앱이 앞에 올 때까지 기다려 그 활동 줄을 돌려줘요. 활동이 바뀌는 순간의 빈 결과는 건너뛰어요. */
async function waitForOtherApp(): Promise<string> {
  let other = "";
  await expect.poll(() => {
    const activity = foregroundActivity();
    other = activity.includes("topResumedActivity") && !activity.includes("dev.thsvkd.jari/") ? activity : "";
    return other;
  }, { timeout: 15_000 }).not.toBe("");
  return other;
}

/** 사용자처럼 뒤로가기로 앱에 돌아와요. 브라우저가 첫 실행 화면 등으로 뒤로가기를 먹으면 앱을 앞으로 불러와요. */
async function returnToApp(): Promise<void> {
  pressAndroidBack();
  const back = await expect
    .poll(foregroundActivity, { timeout: 10_000 })
    .toContain("dev.thsvkd.jari/")
    .then(() => true, () => false);
  if (!back) {
    execFileSync("adb", ["shell", "am", "start", "-n", "dev.thsvkd.jari/com.jari.app.MainActivity"]);
    await expect.poll(foregroundActivity, { timeout: 15_000 }).toContain("dev.thsvkd.jari/");
  }
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
    await waitForOtherApp();
    expect(new URL(page.url()).host).toBe("localhost");

    await returnToApp();
    // WebView 가 시작만 하고 넘겨준 이동을 Playwright 는 끝나지 않은 이동으로 봐서 locator 가 기다려요. 문서를 직접 읽어요.
    await expect
      .poll(() => page.evaluate(() => Boolean(document.querySelector(".screen-settings .policy-links")) && location.host), { timeout: 10_000 })
      .toBe("localhost");
  });

  // 결제는 로그인된 코레일+ 앱에서 해요. 에뮬레이터에는 코레일+ 가 없으니 웹 결제 화면이 브라우저로 열려야 해요.
  test("결제 버튼은 코레일+ 앱이 없으면 코레일 웹을 브라우저로 열고, 앱은 결제 카드 그대로예요", async ({ signedIn: page }) => {
    const korailInstalled = execFileSync("adb", ["shell", "pm", "list", "packages", "com.korail.talk"], { encoding: "utf8" });
    test.skip(korailInstalled.includes("com.korail.talk"), "코레일+ 가 깔린 기기는 앱으로 열어요.");
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    await waitForPaymentCard(page);

    const payLink = page.locator(".screen-home .payment-card a[data-korail-pay]");
    const webHost = new URL((await payLink.getAttribute("href"))!).host;
    execFileSync("adb", ["logcat", "-c"]);
    await payLink.click({ noWaitAfter: true });
    expect(await waitForOtherApp()).not.toContain("com.korail.talk/");
    // 다른 앱이 떴다는 것만으로는 앱 선택 창일 수도 있어요. 코레일+ 를 먼저 묻고, 그다음 코레일 웹 주소를 브라우저에 넘겼는지 봐요.
    const starts = execFileSync("adb", ["logcat", "-d", "-s", "ActivityTaskManager:I"], { encoding: "utf8" })
      .split("\n").filter((line) => line.includes("START u0"));
    const app = starts.findIndex((line) => line.includes("dat=korailtalk://navigation") && line.includes("pkg=com.korail.talk"));
    const web = starts.findIndex((line) => line.includes(`dat=https://${webHost}/`) && line.includes("BROWSABLE"));
    expect(app).toBeGreaterThanOrEqual(0);
    expect(web).toBeGreaterThan(app);
    expect(new URL(page.url()).host).toBe("localhost");

    await returnToApp();
    await expect
      .poll(() => page.evaluate(() => Boolean(document.querySelector(".screen-home .payment-card")) && location.host), { timeout: 10_000 })
      .toBe("localhost");
  });
});
