import { expect, openApp, test } from "./fixtures";
import { expectCleanLayout } from "./layout";
import { keepOnlyTrains, searchTrains, waitForPaymentCard } from "./flows";

// 사용자가 거치는 모든 화면·상태에서 레이아웃 규칙을 재요. phone-dark 프로젝트가 같은 검사를 어두운 테마로 한 번 더 해요.

test.describe("레이아웃 @layout", () => {
  test("로그인 전 화면", async ({ app: page }) => {
    await openApp(page);
    await expectCleanLayout(page, "로그인 방법 고르기");
    await page.locator("[data-auth-gate='guest']").click();
    await expectCleanLayout(page, "초대 회원 로그인");
    await page.locator("[data-auth-mode='register']").click();
    await expectCleanLayout(page, "초대 코드로 가입");
    await page.locator("#auth-form [name='username']").fill("new_friend");
    await page.locator("#auth-form [name='password']").fill("a long secure passphrase");
    await page.locator("#auth-form [name='password_confirm']").fill("a different passphrase");
    await page.locator("#auth-form [name='invite']").fill("apple river cloud");
    await page.locator("#auth-form [name='invite']").press("Enter");
    await expect(page.locator(".form-error")).toBeVisible();
    await expectCleanLayout(page, "가입 비밀번호 불일치");
  });

  test("홈·탭 화면(빈 상태)", async ({ signedIn: page }) => {
    await expectCleanLayout(page, "홈");
    for (const view of ["activity", "favourites", "settings"]) {
      await page.locator(`.bottom-nav [data-view='${view}']`).click();
      await expect(page.locator(`.screen-${view}`)).toBeVisible();
      await expectCleanLayout(page, view);
    }
    await page.locator("[data-view='notifications']").first().click();
    await expectCleanLayout(page, "알림");
  });

  test("새 여정 → 열차 → 좌석표 → 조건 확인", async ({ signedIn: page }) => {
    await page.locator(".screen-home [data-action='new-journey']").click();
    await expectCleanLayout(page, "새 여정");
    await page.locator("[data-dp='toggle']").click();
    await expect(page.locator("[data-dp-day]").first()).toBeVisible();
    await expectCleanLayout(page, "새 여정(달력 펼침)");
    const timeField = (name: string) => page.locator("[data-time-picker]", { has: page.locator(`[name=${name}]`) });
    // 손으로 슬라이더를 끌다 떼요. 떼는 순간 다음 단계로 넘어가요.
    const drag = async (name: string, part: "hour" | "minute", at: number) => {
      // 판을 펼치면 화면이 부드럽게 스크롤돼요. 자리가 멈춘 뒤에 잡아야 옆 슬라이더를 누르지 않아요.
      const slider = timeField(name).locator(`[data-tp-slider='${part}']`);
      // 앞선 레이아웃 검사가 화면을 맨 아래로 내려 두니, 사람처럼 슬라이더가 보이는 데까지 올려요.
      await slider.scrollIntoViewIfNeeded();
      let box = (await slider.boundingBox())!;
      for (let settled = false; !settled;) {
        await page.waitForTimeout(80);
        const next = (await slider.boundingBox())!;
        settled = next.y === box.y;
        box = next;
      }
      await page.mouse.move(box.x + 14, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + 14 + (box.width - 28) * at, box.y + box.height / 2, { steps: 5 });
      await page.mouse.up();
    };
    await timeField("dep_time").locator("[data-tp='toggle']").click();
    await expect(timeField("dep_time").locator("[data-tp-slider='minute']")).toBeVisible();
    await expectCleanLayout(page, "새 여정(dep_time 시간 펼침)");
    await drag("dep_time", "hour", 0.5);
    await expect(timeField("dep_time").locator("[data-tp-slider='minute']")).toBeFocused();
    await drag("dep_time", "minute", 0.5);
    await expect(timeField("dep_time").locator(".time-panel")).toHaveCount(0);
    // 밤 10시가 넘어 돌면 기본값이 "마지막 열차까지"라 끝 시각 칸이 꺼져 있어 넘어가지 않아요.
    if (!(await timeField("max_dep_time").locator("[data-tp='toggle']").isDisabled())) {
      await expect(timeField("max_dep_time").locator("[data-tp-slider='hour']")).toBeFocused();
      await expectCleanLayout(page, "새 여정(max_dep_time 시간 펼침)");
      await drag("max_dep_time", "hour", 0.8);
      await drag("max_dep_time", "minute", 0);
      await expect(timeField("max_dep_time").locator(".time-panel")).toHaveCount(0);
    }
    await page.locator(".bottom-nav [data-view='home']").click();
    await searchTrains(page, { seatMode: "specific", seatClasses: ["general", "special"] });
    await expectCleanLayout(page, "열차 목록(좌석 지정)");
    await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await expectCleanLayout(page, "좌석표");
  });

  test("등급을 가리지 않은 매진 열차는 좌석 지정 하나이고, 창가·복도는 같은 너비예요", async ({ signedIn: page }) => {
    await searchTrains(page);
    const open = page.locator("article.train-card", { has: page.locator("[data-train-toggle='00101']") });
    await expect(open.locator("[data-immediate-any]")).toContainText("바로 예약");
    await expect(open.locator("[data-seat-class]")).toHaveCount(0);

    const soldOut = page.locator("article.train-card", { has: page.locator("[data-train-toggle='00103']") });
    const pick = soldOut.locator("[data-seat-class='general']");
    await expect(pick).toContainText("좌석 지정");
    await expect(pick).not.toContainText("기다릴 좌석");
    await expect(soldOut.locator("[data-seat-class='special']")).toHaveCount(0);
    await pick.click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await expectCleanLayout(page, "좌석 범위(등급 상관없음)");

    const boxes = await page.evaluate(() => {
      const box = (selector: string) => {
        const node = document.querySelector(selector);
        if (!node) return null;
        const rect = node.getBoundingClientRect();
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };
      };
      return {
        windowChip: box("[data-seat-filter='pair:window']"),
        aisleChip: box("[data-seat-filter='pair:aisle']"),
        row: box(".seat-row"),
        mark: box(".train-aisle"),
      };
    });
    expect(boxes.windowChip, "창가 칩").not.toBeNull();
    expect(boxes.aisleChip, "복도 칩").not.toBeNull();
    expect(boxes.row, "좌석 줄").not.toBeNull();
    expect(boxes.mark, "통로 표시").not.toBeNull();
    expect(Math.abs(boxes.windowChip!.width - boxes.aisleChip!.width)).toBeLessThan(2);
    expect(Math.abs(boxes.windowChip!.y - boxes.aisleChip!.y)).toBeLessThan(2);
    expect(boxes.aisleChip!.width).toBeLessThan((boxes.row!.width) * 0.8);
    expect(boxes.mark!.y).toBeGreaterThanOrEqual(boxes.row!.y - 1);
    expect(boxes.mark!.y + boxes.mark!.height).toBeLessThanOrEqual(boxes.row!.y + boxes.row!.height + 1);
  });

  test("좌석 범위는 좌석표가 호차 바로 아래, 조건은 확인 버튼 위이고 열 칩은 A B | C D 한 줄이에요", async ({ signedIn: page }) => {
    await searchTrains(page);
    const soldOut = page.locator("article.train-card", { has: page.locator("[data-train-toggle='00103']") });
    await soldOut.locator("[data-seat-class='general']").click();
    await expect(page.locator(".seat-cell").first()).toBeVisible();
    await expectCleanLayout(page, "좌석 범위(조건 아래)");

    const filters = await page.locator(".seat-dialog [data-seat-filter]:not([data-seat-filter='clear'])").evaluateAll(
      (nodes) => nodes.map((node) => node.getAttribute("data-seat-filter")),
    );
    expect(filters).toEqual(expect.arrayContaining(["col:A", "col:B", "col:C", "col:D", "pair:window", "pair:aisle", "trim:-", "trim:+", "cars:-", "cars:+"]));
    expect(filters).not.toContain("family:only");

    const layout = await page.evaluate(() => {
      const top = (selector: string) => document.querySelector(selector)!.getBoundingClientRect();
      const letters = [...document.querySelectorAll("[data-seat-filter^='col:']")].map((node) => node.getBoundingClientRect());
      return {
        tabs: top(".car-tabs").bottom,
        map: top(".seat-map-live"),
        filter: top(".seat-filter").top,
        confirm: top("[data-action='confirm-seat-dialog']").top,
        letterRows: new Set(letters.map((box) => Math.round(box.y))).size,
        gaps: letters.slice(1).map((box, index) => box.x - letters[index]!.right),
      };
    });
    expect(layout.map.top - layout.tabs, "좌석표는 호차 탭 바로 아래(범례 한 줄만 사이)").toBeLessThan(48);
    expect(layout.map.bottom).toBeLessThanOrEqual(layout.filter);
    expect(layout.filter).toBeLessThan(layout.confirm);
    expect(layout.letterRows, "열 칩 한 줄").toBe(1);
    expect(layout.gaps[1]!, "B와 C 사이 통로").toBeGreaterThan(layout.gaps[0]! + 4);

    await page.locator("[data-seat-filter='pair:window']").click();
    await page.locator("[data-seat-filter='trim:+']").click();
    await page.locator("[data-seat-filter='cars:+']").click();
    await expect(page.locator(".car-tab.trimmed")).toHaveCount(2);
    await expect(page.locator("[data-action='apply-all-cars']")).toBeVisible();
    await expectCleanLayout(page, "좌석 범위(조건 선택)");
  });

  test("찾는 중·결제 대기", async ({ signedIn: page }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await expectCleanLayout(page, "조건 확인");
    await page.locator("[data-action='schedule-toggle']").click();
    await page.locator("[data-time-picker='schedule-time'] [data-tp='toggle']").click();
    await expect(page.locator(".time-panel [data-tp-slider='hour']")).toBeVisible();
    await expectCleanLayout(page, "조건 확인(찾기 시작 시각 펼침)");
    await page.locator("[data-date-picker='schedule-date'] [data-dp='toggle']").click();
    await expect(page.locator(".schedule-panel [data-dp-day]").first()).toBeVisible();
    await expectCleanLayout(page, "조건 확인(찾기 시작 날짜 펼침)");
    await page.locator("[data-action='start-now']").click();
    await expect(page.locator(".screen-home")).toBeVisible();
    await expectCleanLayout(page, "홈(찾는 중)");
    await waitForPaymentCard(page);
    await expectCleanLayout(page, "홈(결제 대기)");
    await page.locator(".bottom-nav [data-view='activity']").click();
    await expectCleanLayout(page, "내 예약(결제 대기)");
  });
});

test("좌석표와 시트는 Escape 로 닫히고, 화면은 그대로예요", async ({ signedIn: page }) => {
  await searchTrains(page, { seatMode: "specific", seatClasses: ["general"] });
  await page.locator("[data-seat-map][data-train-no='00101'][data-seat-class='general']").click();
  await expect(page.locator(".seat-dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator(".seat-dialog")).toHaveCount(0);
  await expect(page.locator(".screen-trains")).toBeVisible();
});
