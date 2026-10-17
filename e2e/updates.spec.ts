import { expect, test, signIn } from "./fixtures";
import { expectCleanLayout } from "./layout";

test("설정에서 앱 업데이트 미지원 상태를 정확히 알려요 @layout", async ({ signedIn: page }, testInfo) => {
  await page.locator(".bottom-nav [data-view='settings']").click();
  const row = page.locator("[data-action='update-check']");
  await expect(row).toBeDisabled();
  await expect(row).toContainText(testInfo.project.name === "device"
    ? "직접 설치용 업데이트 서버가 설정되지 않았어요."
    : "이 환경에서는 앱 내 업데이트를 사용할 수 없어요");
  await expect(row).not.toContainText("최신 버전이에요");
  await expectCleanLayout(page, "설정(앱 업데이트 미지원)");
});

test("Google Play 앱 업데이트의 실패·설치 확인·대기·취소·재시도를 구분해요 @layout", async ({ app: page, user }, testInfo) => {
  test.skip(testInfo.project.name === "device", "브라우저에서만 네이티브 플러그인 대역을 넣어요.");
  await page.addInitScript(() => {
    let checks = 0;
    let applies = 0;
    const surface = window as unknown as {
      Capacitor: unknown;
      cancelInstall: () => void;
      failInstall: () => void;
    };
    surface.Capacitor = {
      PluginHeaders: [{ name: "AppUpdates", methods: ["getStatus", "check", "apply"].map((name) => ({ name, rtype: "promise" })) }],
      nativePromise: async (_plugin: string, method: string) => {
        if (method === "getStatus") {
          return { supported: true, source: "play", currentVersion: `4.22.0-${"x".repeat(90)}`, versionCode: 42201 };
        }
        if (method === "apply") {
          applies += 1;
          return new Promise((resolve, reject) => {
            if (applies === 1) surface.cancelInstall = () => resolve({ status: "cancelled" });
            else surface.failInstall = () => reject(new Error("Google Play에서 앱 설치를 시작하지 못했어요."));
          });
        }
        checks += 1;
        if (checks === 1) throw new Error("Google Play에 연결하지 못했어요. 다시 시도해 주세요.");
        if (checks < 4) return { status: "available", version: `4.23.0-${"x".repeat(110)}`, versionCode: 42300 };
        return { status: "current" };
      },
    };
  });

  await signIn(page, user);
  await page.locator(".bottom-nav [data-view='settings']").click();
  const status = page.locator(".app-update");
  const check = page.locator("[data-action='update-check']");
  await expect(status).toContainText("Google Play");
  await expect(page.getByText("앱 버전", { exact: true }).locator("..")).not.toContainText("42201");
  await expectCleanLayout(page, "Google Play 앱 버전");

  await check.click();
  await expect(status).toContainText("Google Play에 연결하지 못했어요");
  await expect(status).not.toContainText("최신 버전이에요");
  await expectCleanLayout(page, "Google Play 업데이트 확인 실패");

  await check.click();
  const install = page.locator("[data-action='update-apply']");
  await expect(install).toContainText("업데이트 설치");
  await expect(status).not.toContainText("42300");
  await expectCleanLayout(page, "Google Play 업데이트 있음 긴 버전");

  await install.click();
  const sheet = page.locator(".action-sheet");
  await expect(sheet).toContainText("새 버전의 앱을 설치");
  await expect(sheet).toContainText("로그인과 서버의 자리 찾기는 유지");
  await expectCleanLayout(page, "Google Play 업데이트 설치 확인");
  await page.locator("[data-action='sheet-cancel']").click();
  await expect(install).toBeEnabled();

  await install.click();
  await page.locator("[data-action='sheet-confirm']").click();
  await expect(install).toBeDisabled();
  await expect(status).toContainText("Google Play 설치 화면을 준비하고 있어요");
  await expectCleanLayout(page, "Google Play 업데이트 설치 대기");
  await page.evaluate(() => (window as unknown as { cancelInstall: () => void }).cancelInstall());
  await expect(check).toBeEnabled();
  await expect(status).toContainText("업데이트가 취소됐어요");
  await expectCleanLayout(page, "Google Play 업데이트 설치 취소");

  await check.click();
  await expect(install).toBeVisible();
  await install.click();
  await page.locator("[data-action='sheet-confirm']").click();
  await page.evaluate(() => (window as unknown as { failInstall: () => void }).failInstall());
  await expect(status).toContainText("Google Play에서 앱 설치를 시작하지 못했어요");
  await expect(check).toBeEnabled();
  await expectCleanLayout(page, "Google Play 업데이트 설치 실패");

  await check.click();
  await expect(status).toContainText("최신 버전이에요");
  await expectCleanLayout(page, "Google Play 최신 앱 버전");
});

test("직접 설치본은 설치 파일 업데이트 경로를 안내해요 @layout", async ({ app: page, user }, testInfo) => {
  test.skip(testInfo.project.name === "device", "브라우저에서만 네이티브 플러그인 대역을 넣어요.");
  await page.addInitScript(() => {
    let checks = 0;
    (window as unknown as { Capacitor: unknown }).Capacitor = {
      PluginHeaders: [{ name: "AppUpdates", methods: ["getStatus", "check", "apply"].map((name) => ({ name, rtype: "promise" })) }],
      nativePromise: async (_plugin: string, method: string) => {
        if (method === "getStatus") return { supported: true, source: "apk", currentVersion: "4.22.0", versionCode: 42201 };
        if (method === "check") return ++checks === 1
          ? { status: "available", version: "4.23.0", versionCode: 42300 }
          : { status: "current" };
        return { status: "started" };
      },
    };
  });

  await signIn(page, user);
  await page.locator(".bottom-nav [data-view='settings']").click();
  const status = page.locator(".app-update");
  await expect(status).toContainText("새 설치 파일을 내려받아 앱을 업데이트해요");
  await expect(status).not.toContainText("APK");
  await expectCleanLayout(page, "직접 설치 앱 업데이트 경로");
  await page.locator("[data-action='update-check']").click();
  await page.locator("[data-action='update-apply']").click();
  await expect(page.locator(".action-sheet")).toContainText("설치 화면");
  await expect(page.locator(".action-sheet")).toContainText("앱을 다시 열어 주세요");
  await expect(page.locator(".action-sheet")).toContainText("로그인과 서버의 자리 찾기는 유지");
  await expectCleanLayout(page, "직접 설치 앱 업데이트 확인");
  await page.locator("[data-action='sheet-confirm']").click();
  await expect(status).toContainText("설치 화면에서 업데이트를 마친 뒤 앱을 다시 열어 주세요");
  await expect(status).not.toContainText("앱이 다시 열려요");
  await expectCleanLayout(page, "직접 설치 앱 업데이트 시작");
  await page.locator("[data-action='update-check']").click();
  await expect(status).toContainText("최신 버전이에요");
  await expectCleanLayout(page, "APK 최신 앱 버전");
});
