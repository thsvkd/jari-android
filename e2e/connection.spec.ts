import { expect, nextPoll, test } from "./fixtures";
import { expectCleanLayout } from "./layout";

test("연결이 끊기면 오프라인 배너를 띄우고, 돌아오면 다음 폴링을 기다리지 않고 거둔 뒤 못 닿은 요청을 서버에 올려요 @layout", async ({ signedIn: page, context }) => {
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
