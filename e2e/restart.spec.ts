import type { Page } from "@playwright/test";

import { type Control, expect, nextPoll, test } from "./fixtures";
import { keepOnlyTrains, searchTrains, startSearching } from "./flows";
import { expectCleanLayout } from "./layout";

// 운영 배포(docker compose up -d --no-deps api)처럼 API 서버를 SIGTERM 으로 내렸다가 같은 설정으로 다시 띄워요.
// 찾던 검색은 새 서버가 다시 띄운 워커에서 이어져야 하고, 사용자는 "다시 시작했다"는 알림 하나만 받아야 해요.

/** 가짜 코레일에 조회를 보낸 워커들의 pid. 어느 워커가 찾고 있는지는 여기서만 알 수 있어요. */
async function searchingWorkers(control: Control): Promise<number[]> {
  const calls = await control.korailLog();
  return [...new Set(calls.filter((call) => call.event === "search").map((call) => call.pid as number))];
}

/**
 * 앱이 "찾는 중"(healthy)을 보이고, 가짜 코레일에 실제로 조회가 온 워커가 있을 때까지 폴링을 당겨요.
 * 서버가 막 떠서 bootstrap·폴링이 끝나기 전에 상태를 읽으면 안 돼서(느린 CI 에서 그랬어요) 두 가지를 다 기다려요.
 * 찾은 워커의 pid 를 돌려줘요. `besides` 의 워커는 세지 않아요.
 */
async function waitUntilSearching(page: Page, control: Control, besides: number[] = []): Promise<number> {
  const status = page.locator("[data-search-status]");
  for (let attempt = 0; attempt < 30; attempt++) {
    const workers = (await searchingWorkers(control)).filter((pid) => !besides.includes(pid));
    const state = await status.getAttribute("data-state", { timeout: 1_000 }).catch(() => null);
    if (workers.length && state === "healthy") return workers[0]!;
    await page.waitForTimeout(1_000);
    await nextPoll(page);
  }
  await expect(status).toHaveAttribute("data-state", "healthy");
  throw new Error(`재시작 뒤 새 워커의 조회가 없어요. 이전 워커: ${besides.join(", ")}`);
}

async function startSoldOutSearch(page: Page, control: Control): Promise<number> {
  await control.scenario({ search: "sold_out" });
  await searchTrains(page);
  await keepOnlyTrains(page, ["00101"]);
  await startSearching(page);
  await expect(page.locator(".screen-home")).toBeVisible();
  return waitUntilSearching(page, control);
}

/** 알림 화면에는 다시 시작했다는 알림 하나만 있고, 멈췄다·중단됐다·로그인 실패는 없어야 해요. */
async function expectOnlyResumedNotice(page: Page): Promise<void> {
  await page.locator("[data-view='notifications']").first().click();
  await expect(page.locator(".notification", { hasText: "검색을 다시 시작했습니다" })).toHaveCount(1);
  for (const wrong of ["검색이 멈췄습니다", "검색이 중단되었습니다", "로그인 실패", "연결하지 못해"]) {
    await expect(page.locator(".notification", { hasText: wrong })).toHaveCount(0);
  }
}

test.describe("서버를 재시작해도 찾기가 이어져요", () => {
  // 재시작(옛 서버 정리 + 새 서버 시작) 한 번에 몇 초가 들어요.
  test.describe.configure({ timeout: 120_000 });

  test("API 서버가 재시작되면 새 서버의 워커가 다시 로그인해 이어서 찾고, 다시 시작했다는 알림만 와요", async ({ signedIn: page, control }) => {
    const before = await startSoldOutSearch(page, control);

    await control.restart();

    const after = await waitUntilSearching(page, control, [before]);
    const calls = await control.korailLog();
    expect(calls.filter((call) => call.event === "login" && call.pid === after).map((call) => call.accepted)).toEqual([true]);
    await expectOnlyResumedNotice(page);
    await expectCleanLayout(page, "notifications-resumed");
  });

  test("재시작 직후 코레일이 로그인에 한 번 답하지 않아도 다시 로그인해 이어서 찾아요", async ({ signedIn: page, control }) => {
    const before = await startSoldOutSearch(page, control);
    // 이 뒤 첫 로그인(재시작한 워커의 것)에 코레일이 503 으로 답해요.
    await control.scenario({ search: "sold_out", login_unreachable: 1 });

    await control.restart();

    const after = await waitUntilSearching(page, control, [before]);
    const logins = (await control.korailLog()).filter((call) => call.event === "login" && call.pid === after);
    expect(logins.map((call) => [call.accepted, call.unreachable ?? false])).toEqual([[false, true], [true, false]]);
    await expectOnlyResumedNotice(page);
  });
});
