import { E2E } from "../playwright.config";
import { expect, nextPoll, test } from "./fixtures";
import { keepOnlyTrains, searchTrains, trainLine } from "./flows";
import { expectCleanLayout } from "./layout";

// 코레일 공식 예약 대기. 가짜 코레일의 00103 편이 매진이면서 예약 대기 대상(h_wait_rsv_flg " 9")이에요.
// 신청·확인·취소는 운영 코드가 그대로 돌고, 코레일 호출만 fake_korail 이 답해요.

const WAITLIST_TRAIN = "00103";

// 가짜 코레일의 기본 열차에서 00107 도 매진·예약 대기 대상으로 바꾼 목록이에요.
const TWO_WAITLIST_TRAINS = [
  { no: "00101", name: "KTX", group: "KTX", offset: 0, minutes: 161, general: "11", special: "11", wait: -1 },
  { no: "00103", name: "KTX", group: "KTX", offset: 60, minutes: 158, general: "13", special: "13", wait: 9 },
  { no: "00105", name: "KTX-산천", group: "KTX", offset: 120, minutes: 165, general: "13", special: "11", wait: -1 },
  { no: "00107", name: "KTX", group: "KTX", offset: 180, minutes: 160, general: "13", special: "13", wait: 9 },
];

async function applyForWaitlist(page: import("@playwright/test").Page): Promise<void> {
  await searchTrains(page);
  await page.locator(`[data-official-waitlist='${WAITLIST_TRAIN}']`).click();
  await expect(page.locator(".action-sheet")).toContainText("좌석 위치·방향·범위는 고를 수 없어요");
  await page.locator("[data-action='sheet-confirm']").click();
  await expect(page.locator(".screen-home")).toBeVisible();
}

test.describe("코레일 예약 대기", () => {
  test("예약 대기가 되는 매진 열차는 대기 신청이 첫 행동이고, 한계를 알려요 @layout", async ({ signedIn: page }) => {
    await searchTrains(page);
    const card = page.locator(`[data-train-card='${WAITLIST_TRAIN}']`);
    await expect(card.locator(".train-actions > *").first()).toHaveText("코레일 예약 대기 신청");
    // 대기 대상이 아닌 열차에는 대기 신청이 없어요.
    await expect(page.locator("[data-train-card='00105'] [data-official-waitlist]")).toHaveCount(0);
    await expect(page.locator(".waitlist-advice")).toContainText("일반실만");
    await expectCleanLayout(page, "예약 대기를 먼저 권하는 열차 목록");
  });

  test("신청하면 홈에 대기 중으로 보이고, 좌석 확보로 보이지 않아요 @layout", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);

    const card = page.locator("[data-waitlist]");
    await expect(card).toHaveAttribute("data-state", "waiting");
    await expect(card).toContainText("코레일 예약 대기 중");
    await expect(card).toContainText(trainLine(3, WAITLIST_TRAIN, "08:00→10:38"));
    await expect(page.locator(".payment-card")).toHaveCount(0);
    await expectCleanLayout(page, "예약 대기 신청 뒤 홈");
    const log = await control.korailLog();
    expect(log.filter((call) => call.event === "waitlist")).toHaveLength(1);
    // 신청은 한 번뿐이고, 예약(취소표 감시)은 시작하지 않아요.
    expect(log.some((call) => call.event === "search" || String(call.event).startsWith("reserve"))).toBe(false);
  });

  test("좌석이 배정된 것 같으면 대기 카드가 확인과 결제를 안내하고, 결제 카드로 넘기지 않아요 @layout", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);
    await control.scenario({ waitlist: "assigned" });

    const card = page.locator("[data-waitlist]");
    for (let attempt = 0; attempt < 15 && (await card.getAttribute("data-state")) !== "assigned"; attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    await expect(card).toHaveAttribute("data-state", "assigned");
    await expect(card).toContainText("좌석이 배정된 것 같아요. 코레일 앱에서 확인하고 결제해 주세요.");
    await expect(card).toContainText("3호차 7A");
    await expect(card.locator("[data-korail-pay]")).toBeVisible();
    await expect(card.locator("[data-waitlist-cancel]")).toContainText("좌석 돌려주기");
    // 확인되지 않은 배정은 몇 초마다 묻는 결제 감시로 넘기지 않아요.
    await expect(page.locator(".payment-card")).toHaveCount(0);
    await expectCleanLayout(page, "배정 확인이 필요한 예약 대기 카드");
  });

  test("대기 여러 건은 홈과 자세히 보기에 카드로 보여요 @layout", async ({ signedIn: page, control }) => {
    await control.scenario({ trains: TWO_WAITLIST_TRAINS });
    await applyForWaitlist(page);
    await searchTrains(page);
    await page.locator("[data-official-waitlist='00107']").click();
    await page.locator("[data-action='sheet-confirm']").click();
    await expect(page.locator(".screen-home [data-waitlist]")).toHaveCount(2);
    await expectCleanLayout(page, "예약 대기 두 건 홈");

    await page.locator(".bottom-nav [data-view='activity']").click();
    await expect(page.locator("[data-waitlist]")).toHaveCount(2);
    await expect(page.getByText("찾고 있는 자리가 없어요")).toHaveCount(0);
    await expectCleanLayout(page, "자세히 보기의 예약 대기 카드");
  });

  test("다른 코레일 계정으로 신청한 대기는 확인·취소하지 않고 그렇다고 보여요 @layout", async ({ signedIn: page, user, control }) => {
    await applyForWaitlist(page);
    // 사용자가 설정에서 다른 코레일 계정을 연결한 것과 같은 요청이에요.
    const linked = await fetch(`http://127.0.0.1:${E2E.api}/api/mobile/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${user.token}` },
      body: JSON.stringify({ username: "0000000001", password: "korail-e2e" }),
    });
    expect(linked.ok).toBe(true);

    // 다음 확인이 새 계정의 예약 목록을 한 번 읽어 그 대기가 없다는 것을 봐요.
    const card = page.locator("[data-waitlist]");
    for (let attempt = 0; attempt < 15 && (await card.getAttribute("data-state")) !== "other-account"; attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    await expect(card).toHaveAttribute("data-state", "other-account");
    await expect(card).toContainText("지금 연결한 코레일 계정에서 확인할 수 없는 대기예요");
    await expect(card.locator("[data-waitlist-cancel]")).toHaveCount(0);
    await expectCleanLayout(page, "다른 코레일 계정의 예약 대기 카드");
    const calls = await control.korailLog();
    expect(calls.filter((call) => call.event === "waitlist_cancel")).toHaveLength(0);
  });

  test("예약 대기가 남은 채 탈퇴하면 코레일 앱에서 직접 관리하겠다는 동의를 받아요 @layout", async ({ signedIn: page, user, control }) => {
    await applyForWaitlist(page);
    await page.locator(".bottom-nav [data-view='settings']").click();
    await page.locator("[data-action='delete-account']").click();
    await page.locator("#sheet-password").fill(user.password);
    await page.locator("[data-action='sheet-confirm']").click();

    const sheet = page.locator(".action-sheet");
    await expect(sheet).toContainText("코레일 예약 대기가 1건 남아 있어요");
    await expect(sheet).toContainText("코레일 앱에서 직접 확인하고 관리");
    await expectCleanLayout(page, "탈퇴 전 예약 대기 동의");
    await page.locator("[data-action='sheet-confirm']").click();

    await expect(page.locator(".auth-shell")).toBeVisible();
    // 동의했으니 앱은 대기를 지우기만 하고, 코레일에 취소를 보내지 않아요.
    expect((await control.korailLog()).some((call) => call.event === "waitlist_cancel")).toBe(false);
  });

  test("신청한 예약 대기를 취소하면 코레일에 취소를 보내고 취소함으로 보여요 @layout", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);

    await page.locator("[data-waitlist-cancel]").click();
    await expect(page.locator(".action-sheet")).toContainText("코레일 예약 대기를 취소할까요?");
    await page.locator("[data-action='sheet-confirm']").click();

    await expect(page.locator(".toast")).toHaveText("코레일 예약 대기를 취소했어요.");
    await expect(page.locator("[data-waitlist]")).toHaveAttribute("data-state", "cancelled");
    await expectCleanLayout(page, "예약 대기 취소 뒤 홈");
    expect((await control.korailLog()).filter((call) => call.event === "waitlist_cancel")).toHaveLength(1);
  });

  test("코레일이 접속을 막은 것 같으면 대기 확인을 멈추고 그렇다고 보여요 @layout", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);
    await control.scenario({ waitlist: "blocked" });

    const card = page.locator("[data-waitlist]");
    for (let attempt = 0; attempt < 15 && !(await card.textContent())?.includes("접속을 막은"); attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    await expect(card).toContainText("코레일이 접속을 막은 것 같아 확인을 멈췄어요");
    await expect(card).toHaveAttribute("data-state", "waiting");
    await expectCleanLayout(page, "차단 의심으로 멈춘 예약 대기 카드");
    const looked = (await control.korailLog()).filter((call) => call.event === "waitlist_list").length;
    await page.waitForTimeout(3_000);
    expect((await control.korailLog()).filter((call) => call.event === "waitlist_list")).toHaveLength(looked);
  });

  test("예약 대기 확인이 실패해도 끝났다고 하지 않아요", async ({ signedIn: page, control }) => {
    await applyForWaitlist(page);
    await control.scenario({ waitlist: "unavailable" });

    const card = page.locator("[data-waitlist]");
    for (let attempt = 0; attempt < 15 && !(await card.textContent())?.includes("실패"); attempt++) {
      await page.waitForTimeout(1_000);
      await nextPoll(page);
    }
    await expect(card).toContainText("마지막 확인에 실패했어요");
    await expect(card).toHaveAttribute("data-state", "waiting");
  });

  test("예약 대기가 안 되는 매진 열차는 지금처럼 취소표 감시로 찾아요 @layout", async ({ signedIn: page, control }) => {
    // The watch's own search stays sold out too: with seats it would book before the home shows it running.
    await control.scenario({ list: "sold_out", search: "sold_out" });
    await searchTrains(page);
    const card = page.locator("[data-train-card='00101']");
    await expect(card.locator("[data-official-waitlist]")).toHaveCount(0);
    await expect(card.locator("[data-seat-map]")).toBeVisible();
    await keepOnlyTrains(page, ["00101"]);
    await page.locator("[data-action='trains-next']").click();
    await expect(page.locator(".screen-confirm")).toBeVisible();
    await expect(page.locator(".waitlist-advice")).toHaveCount(0);
    await page.locator("[data-action='start-now']").click();
    const sheetConfirm = page.locator("[data-action='sheet-confirm']");
    if (await sheetConfirm.isVisible().catch(() => false)) await sheetConfirm.click();
    await expect(page.locator(".screen-home [data-action='stop-search']")).toBeVisible();
    expect((await control.korailLog()).some((call) => call.event === "waitlist")).toBe(false);
  });

  test("예약 대기가 되는 열차를 감시하려 하면 확인 화면에서 예약 대기를 권해요 @layout", async ({ signedIn: page }) => {
    await searchTrains(page);
    await keepOnlyTrains(page, [WAITLIST_TRAIN]);
    await page.locator("[data-action='trains-next']").click();
    await expect(page.locator(".screen-confirm")).toBeVisible();
    await expect(page.locator(".waitlist-advice")).toContainText("1편은 코레일 예약 대기가 돼요");
    await expectCleanLayout(page, "감시 확인 화면의 예약 대기 권유");
  });
});
