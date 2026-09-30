import { expect, openApp, test } from "./fixtures";
import { expectCleanLayout } from "./layout";

// Play 심사자는 코레일 계정을 연결할 수 없어요. 로그인 화면의 체험하기로 서버 없이 모든 화면을 볼 수 있어야 해요.
test.describe("체험하기", () => {
  test("로그인 화면에서 샘플 데이터로 들어가 열차·좌석표까지 보고, 설정에서 끝내면 로그인 화면으로 돌아와요 @layout", async ({ app: page }) => {
    await openApp(page);
    const gates = page.locator(".gate-card");
    await expect(gates).toHaveCount(3);
    await expect(gates.nth(2)).toContainText("체험하기");
    await expectCleanLayout(page, "로그인 선택(세 가지)");
    // 이 페이지가 체험 중에 낸 요청만 봐요. 앞 스펙이 남긴 서버 쪽 일(결제 감시 등)이 가짜 코레일을 부르는 것은 이 체험과 상관없어요.
    const appOrigin = new URL(page.url()).origin;
    const apiCalls: string[] = [];
    const elsewhere: string[] = [];
    page.on("request", (request) => {
      const url = request.url();
      if (url.includes("/api/mobile")) apiCalls.push(url);
      else if (!url.startsWith("data:") && new URL(url).origin !== appOrigin) elsewhere.push(url);
    });

    await gates.nth(2).click();
    await expect(page.locator(".screen-home")).toBeVisible();
    const banner = page.locator("[data-trial-banner]");
    await expect(banner).toContainText("체험 중");
    await expect(banner).toContainText("실제 예약되지 않아요");
    await expectCleanLayout(page, "홈(체험 중)");

    await page.locator(".bottom-nav [data-view='favourites']").click();
    await page.locator("[data-use-favourite]").first().click();
    await page.locator(".action-sheet [data-action='sheet-confirm']").click();
    await expect(page.locator(".train-list")).toBeVisible();
    await expect(banner).toBeVisible();
    await page.locator("[data-seat-map]").first().click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await expectCleanLayout(page, "좌석표(체험 중)");
    await page.locator("[data-action='close-seat-dialog']").click();

    await page.locator(".bottom-nav [data-view='settings']").click();
    await expect(page.locator("[data-action='delete-account']")).toHaveCount(0);
    await page.locator("[data-action='trial-exit']").click();
    await expect(page.locator(".auth-shell .gate-card")).toHaveCount(3);
    await expect(page.locator("[data-demo-banner]")).toHaveCount(0);
    // 체험은 서버(그 너머의 코레일)에 요청을 하나도 보내지 않고, 앱 자신의 파일 말고는 어디에도 닿지 않아요.
    expect(apiCalls).toEqual([]);
    expect(elsewhere).toEqual([]);
  });
});
