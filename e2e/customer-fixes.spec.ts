import type { Page } from "@playwright/test";

import { expect, nextPoll, test } from "./fixtures";
import { pickDate, pickTime, travelDay } from "./flows";
import { expectCleanLayout } from "./layout";

async function fillJourney(page: Page): Promise<{ date: string }> {
  const date = travelDay(5).iso;
  await page.locator("[name=src_station]").fill("서울");
  await page.locator("[name=dst_station]").fill("부산");
  await pickDate(page, date);
  if (await page.locator("[name=unlimited_time]").isChecked()) {
    await page.locator("label", { has: page.locator("[name=unlimited_time]") }).click();
  }
  await pickTime(page, "dep_time", "07:00");
  await pickTime(page, "max_dep_time", "09:00");
  await page.locator("label", { has: page.locator("[name=train_type][value='2']") }).click();
  await page.locator("label", { has: page.locator("[name=seat_grade_mode][value=specific]") }).click();
  await page.locator("label", { has: page.locator("[name=seat_class][value=general]") }).click();
  await page.locator("label", { has: page.locator("[name=seat_class][value=special]") }).click();
  await page.locator("[data-action=passenger-plus]").click();
  await page.locator("[data-action=passenger-plus]").click();
  await page.locator("label", { has: page.locator("[name=seat_strategy][value='2']") }).click();
  return { date };
}

async function expectJourney(page: Page, date: string): Promise<void> {
  await expect(page.locator("[name=src_station]")).toHaveValue("서울");
  await expect(page.locator("[name=dst_station]")).toHaveValue("부산");
  await expect(page.locator("[name=dep_date]")).toHaveValue(date);
  await expect(page.locator("[name=dep_time]")).toHaveValue("07:00");
  await expect(page.locator("[name=max_dep_time]")).toHaveValue("09:00");
  await expect(page.locator("[name=unlimited_time]")).not.toBeChecked();
  await expect(page.locator("[name=train_type][value='2']")).toBeChecked();
  await expect(page.locator("[name=seat_grade_mode][value=specific]")).toBeChecked();
  await expect(page.locator("[name=seat_class][value=general]")).toBeChecked();
  await expect(page.locator("[name=seat_class][value=special]")).toBeChecked();
  await expect(page.locator(".passenger-row output")).toHaveText("3명");
  await expect(page.locator("[name=seat_strategy][value='2']")).toBeChecked();
}

async function disconnectRailAccount(page: Page): Promise<void> {
  await page.locator(".bottom-nav [data-view=settings]").click();
  await page.locator("[data-view=rail-account]").click();
  await page.locator("[data-action=rail-logout]").click();
  await page.locator(".action-sheet [data-action=sheet-confirm]").click();
  await expect(page.locator("#rail-form")).toBeVisible();
}

async function assertShortcutGutterAfterScroll(page: Page): Promise<void> {
  const shelf = page.locator(".idle-shelf");
  const chips = shelf.locator("[data-route-chip]");
  await expect(chips).toHaveCount(5);
  expect(await shelf.evaluate((node) => node.scrollWidth > node.clientWidth)).toBe(true);

  const gutterError = async () => page.evaluate(() => {
    const screen = document.querySelector<HTMLElement>(".screen-home")!;
    const chip = document.querySelector<HTMLElement>(".idle-shelf [data-route-chip]")!;
    const edge = screen.getBoundingClientRect().left + parseFloat(getComputedStyle(screen).paddingLeft);
    return Math.abs(chip.getBoundingClientRect().left - edge);
  });
  await expect.poll(gutterError).toBeLessThanOrEqual(1);

  await chips.nth(3).evaluate((node) => {
    node.scrollIntoView({ behavior: "instant", block: "nearest", inline: "start" });
    node.focus();
  });
  await expect.poll(() => shelf.evaluate((node) => node.scrollLeft)).toBeGreaterThan(0);

  // 키보드·접근성 초점으로 네 번째 칩까지 이동한 뒤 처음으로 돌아와도 첫 칩은 화면 여백선에 맞아야 해요.
  await shelf.evaluate((node) => node.scrollTo({ left: 0, behavior: "instant" }));
  await expect.poll(() => shelf.evaluate((node) => node.scrollLeft)).toBeLessThanOrEqual(1);
  await expect.poll(gutterError).toBeLessThanOrEqual(1);
}

test.describe("고객 불편 회귀", () => {
  test("코레일 계정을 연결하러 다녀와도 입력한 여정을 그대로 이어가요 @layout", async ({ signedIn: page }) => {
    await disconnectRailAccount(page);
    await page.locator(".bottom-nav [data-view=home]").click();
    await page.locator(".screen-home [data-action=new-journey]").click();
    const { date } = await fillJourney(page);

    await page.locator(".bottom-nav [data-view=settings]").click();
    await page.locator("[data-view=rail-account]").click();
    await page.locator("#rail-form [name=username]").fill("0000000000");
    await page.locator("#rail-form [name=password]").fill("korail-e2e");
    await page.locator("#rail-form [name=password]").press("Enter");
    await expect(page.locator(".screen-settings")).toBeVisible();

    await page.locator(".bottom-nav [data-view=home]").click();
    await expect(page.locator("[data-action=new-journey]")).toContainText("입력하던 여정 이어서 찾기");
    await page.locator("[data-action=new-journey]").click();
    await expectJourney(page, date);
    await expectCleanLayout(page, "새 여정(코레일 계정 연결 뒤 복원)");
  });

  test("세션이 만료돼 같은 사용자로 다시 로그인하면 입력한 여정을 복원해요 @layout", async ({ signedIn: page, user }, testInfo) => {
    test.skip(testInfo.project.name === "device", "기기 WebView에서는 브라우저 요청 가로채기를 사용하지 않아요.");
    await page.locator(".screen-home [data-action=new-journey]").click();
    const { date } = await fillJourney(page);

    let rejected = false;
    await page.route("**/api/mobile/status", async (route) => {
      if (rejected) return route.continue();
      rejected = true;
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ error: "세션이 만료됐어요." }),
      });
    });
    await nextPoll(page);
    await expect(page.locator(".auth-shell")).toBeVisible();
    await page.unroute("**/api/mobile/status");

    await page.locator("[data-auth-gate=guest]").click();
    await page.locator("#auth-form [name=username]").fill(user.username);
    await page.locator("#auth-form [name=password]").fill(user.password);
    await page.locator("#auth-form [name=password]").press("Enter");
    await expect(page.locator(".screen-journey")).toBeVisible();
    await expectJourney(page, date);
    await expectCleanLayout(page, "새 여정(세션 만료 뒤 복원)");
  });

  test("최근 구간이 넘쳐도 처음으로 돌아오면 첫 칩이 화면 여백선에 맞아요 @layout", async ({ signedIn: page }) => {
    await page.evaluate(() => {
      const route = (src_station: string, dst_station: string, dep_time: string) => ({
        v: 1,
        action: "prepare_search",
        dep_date: "20261020",
        src_station,
        dst_station,
        dep_time,
        max_dep_time: "2200",
        train_type: "1",
        seat_option: "1",
        passenger_count: 1,
        seat_strategy: "1",
        seat_preference: "",
      });
      localStorage.setItem("jari.recentRoutes", JSON.stringify([
        route("수서", "목포", "0700"),
        route("광명", "대전", "0800"),
        route("용산", "익산", "0900"),
        route("동대구", "진주", "1000"),
        route("울산", "포항", "1100"),
      ]));
    });

    // 저장소 변경은 다음 홈 렌더에서 읽어요. 같은 화면을 다시 그린 뒤에도 스크롤 시작점 여백을 반복 검증해요.
    for (let render = 0; render < 2; render += 1) {
      await page.locator(".bottom-nav [data-view=settings]").click();
      await page.locator(".bottom-nav [data-view=home]").click();
      await assertShortcutGutterAfterScroll(page);
    }
    await expectCleanLayout(page, "홈(최근 구간 5개 가로 스크롤)");
  });
});
