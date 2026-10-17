import { expect, test, signIn } from "./fixtures";
import { expectCleanLayout } from "./layout";

test("설정에서 앱 내 업데이트 지원 여부를 정확히 알려요 @layout", async ({ signedIn: page }) => {
  await page.locator(".bottom-nav [data-view='settings']").click();
  const row = page.locator("[data-action='update-check']");
  await expect(row).toBeDisabled();
  await expect(row).toContainText("이 환경에서는 앱 내 업데이트를 사용할 수 없어요");
  await expectCleanLayout(page, "설정(업데이트 미지원)");
});

test("로그인 전에도 업데이트 확정 실패를 알려요 @layout", async ({ app: page }, testInfo) => {
  test.skip(testInfo.project.name === "device", "브라우저에서만 네이티브 오류 대역을 넣어요.");
  await page.addInitScript(() => {
    (window as unknown as { Capacitor: unknown }).Capacitor = {
      PluginHeaders: [{ name: "AppUpdates", methods: ["getStatus", "ready"].map((name) => ({ name, rtype: "promise" })) }],
      nativePromise: async (_plugin: string, method: string) => {
        if (method === "ready") throw new Error("State commit failed");
        return { bootId: "boot-1", supported: false, currentVersion: "4.21.0", nativeVersion: "4.21.0" };
      },
    };
  });
  await page.goto("/");
  await expect(page.locator(".auth-shell .update-ready-error")).toContainText("업데이트 상태를 확정하지 못했어요");
  await expectCleanLayout(page, "로그인 전 업데이트 확정 실패");
});

// Browser bridge double exercises UI only. Java tests and the device stage own native verification.
test("업데이트 확인 실패·적용·최신·APK 필요 상태를 구분해요 @layout", async ({ app: page, user }, testInfo) => {
  test.skip(testInfo.project.name === "device", "네이티브 플러그인을 브라우저 대역으로 바꾸지 않아요.");
  await page.addInitScript(() => {
    let checks = 0;
    const surface = window as unknown as {
      Capacitor: unknown;
      failUpdate: () => void;
    };
    surface.Capacitor = {
      PluginHeaders: [{ name: "AppUpdates", methods: ["getStatus", "check", "apply", "ready"].map((name) => ({ name, rtype: "promise" })) }],
      nativePromise: async (_plugin: string, method: string, options?: { bootId?: string }) => {
        if (method === "getStatus") return { bootId: "boot-1", supported: true, currentVersion: `4.21.0-web.1-${"x".repeat(116)}`, nativeVersion: "4.21.0" };
        if (method === "ready") {
          if (options?.bootId !== "boot-1") throw new Error("Stale page readiness");
          return;
        }
        if (method === "apply") return new Promise((_resolve, reject) => { surface.failUpdate = () => reject(new Error("업데이트 파일을 확인하지 못했어요. 다시 확인해 주세요.")); });
        checks++;
        if (checks === 1) throw new Error("업데이트 서버에 연결하지 못했어요. 다시 시도해 주세요.");
        return checks === 2 ? { status: "available", version: `4.21.0-web.2-${"x".repeat(116)}` } : checks === 3 ? { status: "current" } : { status: "incompatible", version: "5.0.0" };
      },
    };
  });
  await signIn(page, user);
  await page.locator(".bottom-nav [data-view='settings']").click();
  const check = page.locator("[data-action='update-check']");
  const status = page.locator(".app-update");
  await expect(status).toContainText("4.21.0-web.1");
  await check.click();
  await expect(status).toContainText("업데이트 서버에 연결하지 못했어요");
  await expectCleanLayout(page, "업데이트 확인 실패");
  await check.click();
  const apply = page.locator("[data-action='update-apply']");
  await expect(status).toContainText("4.21.0-web.2");
  await expectCleanLayout(page, "업데이트 있음");
  await apply.click();
  await expect(page.locator(".action-sheet")).toContainText("다시 열려요");
  await expectCleanLayout(page, "업데이트 적용 확인");
  await page.locator("[data-action='sheet-cancel']").click();
  await expect(apply).toBeEnabled();
  await apply.click();
  await page.locator("[data-action='sheet-confirm']").click();
  await expect(apply).toBeDisabled();
  await expectCleanLayout(page, "업데이트 적용 중");
  await page.evaluate(() => (window as unknown as { failUpdate: () => void }).failUpdate());
  await expect(status).toContainText("업데이트 파일을 확인하지 못했어요");
  await expectCleanLayout(page, "업데이트 적용 실패");
  await check.click();
  await expect(status).toContainText("최신 버전이에요");
  await expectCleanLayout(page, "최신 업데이트");
  await check.click();
  await expect(status).toContainText("앱 설치 파일을 업데이트해 주세요");
  await expect(apply).toHaveCount(0);
  await expectCleanLayout(page, "APK 업데이트 필요");
});
