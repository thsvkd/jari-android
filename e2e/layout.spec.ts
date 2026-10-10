import { expect, openApp, test } from "./fixtures";
import { expectCleanLayout } from "./layout";
import { keepOnlyTrains, searchTrains, waitForPaymentCard } from "./flows";

// 사용자가 거치는 모든 화면·상태에서 레이아웃 규칙을 재요. phone-dark 프로젝트가 같은 검사를 어두운 테마로 한 번 더 해요.

// 기기 에뮬레이터(소프트웨어 GPU)는 시간 휠을 넘기는 장면에서 말없이 죽는 일이 잦아요(호스트 쪽 렌더러 결함, 프로세스가 통째로 사라져 뒤 테스트가 전부 연쇄 실패해요).
// 그래서 기기 단계의 필수 묶음에서는 휠 장면을 빼고, 헤드리스 세 프로젝트가 계속 재요. 기기에서 휠도 보려면 JARI_DEVICE_WHEEL=1(야간·릴리스 때의 전체 실행)을 줘요.
const wheelHere = (project: string) => project !== "device" || process.env.JARI_DEVICE_WHEEL === "1";

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

  test("새 여정의 시간 휠", async ({ signedIn: page }, testInfo) => {
    test.skip(!wheelHere(testInfo.project.name), "기기 에뮬레이터에서는 야간·릴리스 전체 실행에서만 재요");
    await page.locator(".screen-home [data-action='new-journey']").click();
    await expectCleanLayout(page, "새 여정");
    // 원래 순서 그대로, 달력이 펼쳐진 채로 시간 칸을 열어요.
    await page.locator("[data-dp='toggle']").click();
    await expect(page.locator("[data-dp-day]").first()).toBeVisible();
    const timeField = (name: string) => page.locator("[data-time-picker]", { has: page.locator(`[name=${name}]`) });
    // 손가락처럼 휠 위에서 위아래로 넘겨요. 멈추면 가운데 띠에 든 정시가 값이 되고, 칸은 열린 채예요.
    const flick = async (name: string, rows: number) => {
      const wheel = timeField(name).locator("[data-tp-wheel]");
      // 판을 펼치면 화면이 부드럽게 스크롤돼요. 자리가 멈춘 뒤에 휠 위로 가요.
      await wheel.scrollIntoViewIfNeeded();
      let box = (await wheel.boundingBox())!;
      for (let settled = false; !settled;) {
        await page.waitForTimeout(80);
        const next = (await wheel.boundingBox())!;
        settled = next.y === box.y;
        box = next;
      }
      const before = await page.locator(`[name=${name}]`).inputValue();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, rows * 48);
      await expect(page.locator(`[name=${name}]`)).not.toHaveValue(before);
      await expect(page.locator(`[name=${name}]`)).toHaveValue(/^\d\d:00$/);
      // 가운데 띠의 글자가 고른 시각이에요.
      const value = await page.locator(`[name=${name}]`).inputValue();
      await expect(timeField(name).locator(".time-option.current")).toHaveAttribute("data-tp-hour", String(Number(value.slice(0, 2))));
      await expect(timeField(name).locator(".time-panel")).toBeVisible();
    };
    await timeField("dep_time").locator("[data-tp='toggle']").click();
    await expect(timeField("dep_time").locator("[data-tp-wheel]")).toBeVisible();
    await expect(timeField("dep_time").locator("[data-tp-wheel]")).toBeFocused();
    await expectCleanLayout(page, "새 여정(dep_time 시간 펼침)");
    // 기본 시작은 지금 시각의 정시라 밤 9시가 넘으면 아래로 두 줄 넘길 자리가 없어요. 그때는 위로 넘겨요.
    const startHour = Number((await page.locator("[name=dep_time]").inputValue()).slice(0, 2));
    await flick("dep_time", startHour >= 21 ? -2 : 2);
    // 한 줄을 누르면 그 시각으로 정하고 끝 칸으로 넘어가요.
    await timeField("dep_time").locator("[data-tp-hour='8']").click();
    await expect(page.locator("[name=dep_time]")).toHaveValue("08:00");
    await expect(timeField("dep_time").locator(".time-panel")).toHaveCount(0);
    // 밤 10시가 넘어 돌면 기본값이 "마지막 열차까지"라 끝 시각 칸이 꺼져 있어 넘어가지 않아요.
    if (!(await timeField("max_dep_time").locator("[data-tp='toggle']").isDisabled())) {
      await expect(timeField("max_dep_time").locator("[data-tp-wheel]")).toBeFocused();
      await expectCleanLayout(page, "새 여정(max_dep_time 시간 펼침)");
      await timeField("max_dep_time").locator("[data-tp-hour='18']").click();
      await expect(page.locator("[name=max_dep_time]")).toHaveValue("18:00");
      await expect(timeField("max_dep_time").locator(".time-panel")).toHaveCount(0);
    }
  });

  test("새 여정 → 열차 → 좌석표 → 조건 확인", async ({ signedIn: page }) => {
    await page.locator(".screen-home [data-action='new-journey']").click();
    await expectCleanLayout(page, "새 여정");
    await page.locator("[data-dp='toggle']").click();
    await expect(page.locator("[data-dp-day]").first()).toBeVisible();
    await expectCleanLayout(page, "새 여정(달력 펼침)");
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

  test("찾는 중·결제 대기", async ({ signedIn: page }, testInfo) => {
    await searchTrains(page);
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await expectCleanLayout(page, "조건 확인");
    await page.locator("[data-action='schedule-toggle']").click();
    if (wheelHere(testInfo.project.name)) {
      await page.locator("[data-time-picker='schedule-time'] [data-tp='toggle']").click();
      await expect(page.locator(".time-panel [data-tp-wheel]")).toBeVisible();
      await expectCleanLayout(page, "조건 확인(찾기 시작 시각 펼침)");
    }
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
