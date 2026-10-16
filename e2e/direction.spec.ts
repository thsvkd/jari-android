import { expect, test } from "./fixtures";
import { expectCleanLayout } from "./layout";
import { pickDate, pickTime, travelDay } from "./flows";

test("역방향 조건을 확인하고 자동 예약 작업자까지 전달해요 @layout", async ({ signedIn: page, control }) => {
  await page.locator("[data-action='new-journey']").click();
  await pickDate(page, travelDay().iso);
  await pickTime(page, "dep_time", "07:00");
  await page.locator("[name='unlimited_time']").check();
  await page.locator("label", { has: page.locator("[name='seat_direction'][value='backward']") }).click();
  await expectCleanLayout(page, "좌석 방향 선택");
  await page.getByRole("button", { name: /열차 조회하기/ }).click();
  await expect(page.locator("[data-train-card]").first()).toBeVisible();
  await expect(page.locator("[data-seat-map], [data-official-waitlist]")).toHaveCount(0);
  await expectCleanLayout(page, "방향 조건의 열차 선택");
  await page.locator("[data-immediate-any='00101']").click();
  await expect(page.getByRole("dialog")).toContainText("역방향");
  await expectCleanLayout(page, "역방향 예약 확인");
  await page.locator("[data-action='sheet-confirm']").click();
  await expect.poll(async () => (await control.korailLog()).filter((entry) => entry.event === "reserve_train")).not.toHaveLength(0);
  const calls = await control.korailLog();
  expect(calls.some((entry) => entry.seat_direction === "010")).toBe(true);
});
