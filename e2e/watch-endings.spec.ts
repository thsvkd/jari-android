import { expect, nextPoll, test } from "./fixtures";
import { keepOnlyTrains, searchTrains, startSearching } from "./flows";
import { expectCleanLayout } from "./layout";
import type { Page } from "@playwright/test";

// 감시가 스스로 멈추는 세 가지. 코레일만 가짜이고 워커·알림은 운영 코드예요.
// 멈춘 감시는 매진·예약·오류와 다른 알림으로 보여야 하고, 홈에는 찾는 중으로 남으면 안 돼요.

/** 워커가 멈출 때까지 앱의 폴링을 당겨요. 멈추면 홈이 빈 상태로 돌아와요. */
async function waitUntilStopped(page: Page): Promise<void> {
  const status = page.locator(".screen-home [data-search-status]");
  for (let attempt = 0; attempt < 30; attempt++) {
    await page.waitForTimeout(1_000);
    await nextPoll(page);
    if ((await status.getAttribute("data-state", { timeout: 1_000 }).catch(() => null)) === "idle") return;
  }
  await expect(status).toHaveAttribute("data-state", "idle");
}

for (const ending of [
  {
    name: "코레일이 알아볼 수 없는 응답을 주면(차단 의심) 첫 응답에서 멈추고",
    scenario: { search: "blocked" as const },
    kind: "접속 제한",
    title: "코레일이 접속을 막은 것 같아 감시를 멈췄어요",
    body: "코레일 앱에서 로그인되는지 확인해 주세요",
  },
  {
    name: "조회 실패가 한도만큼 이어지면 멈추고",
    scenario: { search: "unavailable" as const, failure_stop_seconds: 1 },
    kind: "응답 없음",
    title: /^코레일 응답이 .+째 없어 감시를 멈췄어요$/,
    body: "좌석이 없다는 뜻이 아니에요",
  },
  {
    name: "감시 시간 한도가 지나면 멈추고",
    scenario: { search: "sold_out" as const, watch_max_seconds: 2 },
    kind: "시간 끝",
    title: /동안 좌석을 잡지 못해 감시를 멈췄어요$/,
    body: "더 기다리려면 새로 찾아 주세요",
  },
]) {
  test(`${ending.name} 그 이유를 따로 알려요 @layout`, async ({ signedIn: page, control }) => {
    await control.scenario(ending.scenario);
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await startSearching(page);
    await expect(page.locator(".screen-home")).toBeVisible();

    await waitUntilStopped(page);
    // 찾는 중도, 결제 카드도 아니에요.
    await expect(page.locator(".screen-home [data-search-status]")).toHaveAttribute("data-state", "idle");
    await expect(page.locator(".payment-card")).toHaveCount(0);
    await expectCleanLayout(page, `감시 멈춤 홈 ${ending.kind}`, { noPageScroll: true });

    await page.locator("[data-view='notifications']").first().click();
    const notice = page.locator(".notification").first();
    await expect(notice.locator(".notification-icon")).toHaveAttribute("aria-label", ending.kind);
    await expect(notice.locator(".notification-head b")).toHaveText(ending.title);
    await expect(notice.locator(".notification-detail")).toHaveText("서울 → 부산");
    await expect(notice.locator(".notification-text")).toContainText(ending.body);
    // 예약·문제 알림으로 보이면 안 돼요.
    await expect(page.locator(".notification-icon[aria-label='예약'], .notification-icon[aria-label='문제']")).toHaveCount(0);
    await expectCleanLayout(page, `감시 멈춤 알림 ${ending.kind}`);

    const searches = (await control.korailLog()).filter((call) => call.event === "search");
    if (ending.scenario.search === "blocked") expect(searches.map((call) => call.mode)).toEqual(["blocked"]);
    else expect(searches.length).toBeGreaterThan(0);
  });
}
