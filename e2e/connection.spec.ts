import { expect, nextPoll, test } from "./fixtures";
import { expectCleanLayout } from "./layout";

test("연결이 끊기면 오프라인 배너를 띄우고, 돌아오면 다음 폴링을 기다리지 않고 거둔 뒤 못 닿은 요청을 서버에 올려요 @layout", async ({ signedIn: page, context }) => {
  // 홈은 bootstrap 전에도 그려져요. 상태 카드는 bootstrap 이 끝나 30초 폴링이 걸린 뒤에야 나와요.
  // 그 전에 시계를 당기면 당길 폴링이 아직 없어요(느린 CI 에서 그랬어요).
  await expect(page.locator("[data-search-status]")).toBeVisible();
  await context.setOffline(true);
  await nextPoll(page);
  await expect(page.locator(".offline-banner")).toBeVisible();
  await expectCleanLayout(page, "home-offline");

  const upload = page.waitForRequest((request) => request.url().endsWith("/api/mobile/diagnostics") && request.method() === "POST");
  // 다음 30초 폴링은 기다림(10초)보다 멀어요. 배너가 걷히는 것은 online 이벤트에 바로 다시 물어서예요.
  await context.setOffline(false);
  await expect(page.locator(".offline-banner")).toBeHidden();
  const { misses } = (await upload).postDataJSON() as { misses: Array<Record<string, unknown>> };
  expect(misses).toEqual([expect.objectContaining({ method: "GET", path: "/status", reason: "network", online: false, visible: true })]);
  expect((await (await upload).response())?.status()).toBe(200);
});
