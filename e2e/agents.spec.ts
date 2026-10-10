import { createHash, randomBytes } from "node:crypto";

import type { Locator, Page } from "@playwright/test";

import { E2E } from "../playwright.config";
import { expect, openApp, test, type User } from "./fixtures";
import { expectCleanLayout } from "./layout";

// 에이전트 연결(명세 .omc/plans/mcp-agent-spec.md §7). 대기 요청은 실제 MCP 클라이언트처럼
// /oauth/register 와 /oauth/authorize 를 불러 만들어요. 스택은 MOBILE_PUBLIC_URL 을 API 주소로 띄워요.
const API = `http://127.0.0.1:${E2E.api}`;
const MCP_URL = `${API}/api/mobile/mcp`;
const CLAUDE = "https://claude.ai/api/mcp/auth_callback";
const LOOPBACK = "http://127.0.0.1:53682/callback";
const WARNING = "직접 연 브라우저에 나온 코드만 입력해 주세요. 다른 사람이 알려 준 코드라면 거절해 주세요.";
// §12 S-H1: 승인 성공 토스트와 국가 불일치 경고.
// §15 N2: 승인 직후에는 아직 끝나지 않았다고 알리고, 연결이 목록에 나타나면 그때 연결했다고 알려요.
const APPROVED = "승인했어요. 브라우저로 돌아가 연결을 마쳐 주세요.";
const CONNECTED = "연결했어요. 예약까지 맡기려면 아래 목록에서 켜 주세요.";
const STALLED = "연결이 끝나지 않았어요. 에이전트에서 연결을 다시 시작해 주세요.";
// §15 N7: 에이전트가 확인을 받는다는 말은 서버가 강제하지 않으니 낮춰서 말해요.
const ASKS_FIRST = "Claude Code 같은 에이전트는 감시·예약·취소 전에 먼저 물어봐요.";
const ELSEWHERE = "코드를 요청한 브라우저가 지금 휴대폰과 다른 나라에 있어요. 직접 요청한 게 아니라면 거절해 주세요.";
const CODE = /\b([ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4})-([ABCDEFGHJKMNPQRSTUVWXYZ23456789]{4})\b/;

interface PendingRequest {
  code: string;
  cookie: string;
  clientId: string;
  verifier: string;
  redirect: string;
}

/** 에이전트가 브라우저를 연 상태. 화면에 나온 코드와 그 브라우저의 쿠키예요. */
async function pendingRequest(name = "e2e 에이전트", redirect = CLAUDE, country?: string): Promise<PendingRequest> {
  const registered = await fetch(`${API}/oauth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ redirect_uris: [redirect], client_name: name }),
  });
  expect(registered.status).toBe(201);
  const { client_id: clientId } = (await registered.json()) as { client_id: string };
  const verifier = randomBytes(48).toString("base64url");
  const query = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirect,
    code_challenge: createHash("sha256").update(verifier).digest("base64url"),
    code_challenge_method: "S256",
    state: "e2e-state",
    resource: MCP_URL,
    scope: "jari.read jari.book",
  });
  // Cloudflare 가 붙이는 국가 헤더를 흉내 내요. 로컬(헤더 없음)이면 앱은 위치를 보여 주지 않아요.
  const page = await fetch(`${API}/oauth/authorize?${query}`, { headers: country ? { "CF-IPCountry": country } : {} });
  expect(page.status).toBe(200);
  const match = CODE.exec(await page.text());
  expect(match, "인가 페이지에 코드가 없어요").not.toBeNull();
  const cookie = (page.headers.getSetCookie().find((header) => header.startsWith("jari_oauth=")) ?? "").split(";")[0]!;
  return { code: `${match![1]}-${match![2]}`, cookie, clientId, verifier, redirect };
}

/** 사용자가 앱에서 먼저 확보한 요청의 id. 브라우저 poll 이 쓰는 값과 같아요(§11 L13). 같은 사용자는 앱에서 다시 찾을 수 있어요. */
async function requestIdOf(user: User, code: string): Promise<string> {
  const reply = await fetch(`${API}/api/mobile/agents/requests/lookup`, {
    method: "POST",
    headers: { Authorization: `Bearer ${user.token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  expect(reply.status).toBe(200);
  return ((await reply.json()) as { requestId: string }).requestId;
}

/** 승인 뒤 에이전트 쪽 마무리: 브라우저 poll 로 코드를 받아 토큰으로 바꿔요. 이게 끝나야 연결이 목록에 나와요(§11 L4). */
async function finishInBrowser(pending: PendingRequest, requestId: string): Promise<{ access_token: string }> {
  const polled = await fetch(`${API}/oauth/authorize/poll?${new URLSearchParams({ request: requestId })}`, {
    headers: { Cookie: pending.cookie },
  });
  expect(polled.status).toBe(200);
  const body = (await polled.json()) as { status: string; redirect: string };
  expect(body.status).toBe("approved");
  const code = new URL(body.redirect).searchParams.get("code")!;
  const exchanged = await fetch(`${API}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: pending.redirect,
      client_id: pending.clientId,
      code_verifier: pending.verifier,
      resource: MCP_URL,
    }),
  });
  expect(exchanged.status).toBe(200);
  return (await exchanged.json()) as { access_token: string };
}

/** 앱에서 연결하기를 누르고, 교환 전에는 목록에 없음을 확인한 뒤 브라우저 쪽을 마치고 화면을 다시 열어요. */
const FINISHING = "브라우저에서 연결을 마치는 중이에요";

/**
 * 앱에서 연결하기를 누르고, 교환 전에는 목록에 없음을 확인한 뒤 브라우저 쪽을 마쳐요.
 * §13 U9: 앱은 기다리는 동안 상태 문구를 보이고 2초마다 목록을 다시 읽어요. 화면을 다시 열지 않아요.
 */
async function approveAndExchange(page: Page, user: User, pending: PendingRequest, requestId: string): Promise<void> {
  const before = (await connections(user)).length;
  await page.locator(".action-sheet").getByRole("button", { name: "연결하기" }).click();
  await expect(page.locator(".toast")).toHaveText(APPROVED);
  await expect(page.getByText(FINISHING)).toBeVisible();
  // 승인만으로는 목록에 늘지 않아요(§11 L4). 앞서 연결한 것은 그대로예요.
  expect(await connections(user)).toHaveLength(before);
  await finishInBrowser(pending, requestId);
  await advanceUntil(page, async () => (await page.getByText(FINISHING).count()) === 0);
  await expect(page.locator(".toast")).toHaveText(CONNECTED);
  expect(await connections(user)).toHaveLength(before + 1);
}

/**
 * 가상 시계를 2초씩 돌리며 조건이 맞을 때까지 기다려요. 앱의 목록 다시 읽기는 응답이 와야 다음 차례가
 * 이어지는데, 느린 기기에서는 응답이 가상 시계보다 늦어요. 한 번에 크게 돌리면 그 사이 타이머가 울리지 않아요.
 * 20초 동안 0.5초마다 돌리므로 가상 시계는 많아야 80초쯤 가요(2분 한도 아래).
 */
async function advanceUntil(page: Page, check: () => Promise<boolean> | boolean): Promise<void> {
  await expect
    .poll(
      async () => {
        if (await check()) return true;
        await page.clock.runFor(2_000);
        return check();
      },
      { intervals: [500], timeout: 20_000 },
    )
    .toBe(true);
}

/** 보이게 될 때까지 기다린 뒤 화면에서의 위쪽 좌표. 다시 그려지는 중이면 null 이 나와서 기다려요. */
async function top(locator: Locator): Promise<number> {
  let y: number | undefined;
  await expect.poll(async () => (y = (await locator.boundingBox())?.y)).toBeDefined();
  return y!;
}

interface Connection {
  id: string;
  name: string;
  allowBooking: boolean;
  createdAt: string;
}

async function connections(user: User): Promise<Connection[]> {
  const reply = await fetch(`${API}/api/mobile/agents`, { headers: { Authorization: `Bearer ${user.token}` } });
  expect(reply.status).toBe(200);
  return ((await reply.json()) as { agents: Connection[] }).agents;
}

/** 앱의 lookup 요청에 Cloudflare 국가 헤더를 실어요. 페이지가 붙인 헤더가 아니라 CORS 사전 요청이 생기지 않아요. */
async function phoneIn(page: Page, country: string): Promise<void> {
  await page.route(`${API}/api/mobile/agents/requests/lookup`, (route) =>
    route.continue({ headers: { ...route.request().headers(), "cf-ipcountry": country } }),
  );
}

const countryName = (code: string) => new Intl.DisplayNames("ko", { type: "region" }).of(code) ?? code;

async function openAgents(page: Page): Promise<void> {
  await page.locator(".bottom-nav [data-view='settings']").click();
  const row = page.getByRole("button", { name: /에이전트 연결/ });
  await expect(row).toContainText("AI 에이전트가 열차를 찾고 예약하게 해요");
  await row.click();
  await expect(page.getByLabel("브라우저에 나온 8자리 코드")).toBeVisible();
}

function codeInput(page: Page) {
  return page.getByLabel("브라우저에 나온 8자리 코드");
}

async function enterCode(page: Page, code: string): Promise<void> {
  await codeInput(page).fill("");
  await codeInput(page).pressSequentially(code);
  await page.getByRole("button", { name: "확인", exact: true }).click();
}

test.describe("에이전트 연결", () => {
  test("설정에서 연결 화면을 열면 넣을 주소와 복사 버튼이 있어요 @layout", async ({ signedIn: page }) => {
    await openAgents(page);
    // 기기 WebView(connectOverCDP)에서는 권한을 줄 수 없어요(grantPermissions 미지원). 클립보드 쓰기를 감싸
    // 앱이 복사한 값을 기록해요. 모든 프로젝트에서 같은 방식이에요.
    await page.evaluate(() => {
      const copied: string[] = [];
      (window as unknown as { __copied: string[] }).__copied = copied;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (text: string) => void copied.push(text) },
      });
    });
    await expect(page.getByText(MCP_URL, { exact: false }).first()).toBeVisible();
    // §13 U10: 주소를 넣은 뒤 에이전트에서 연결을 시작해야 코드가 나와요(Claude Code 는 /mcp).
    // MCP 주소(…/api/mobile/mcp)가 아니라 명령으로서의 /mcp 예요.
    await expect(page.locator("main.screen")).toContainText(/(?<![\w./:-])\/mcp\b/);
    await expectCleanLayout(page, "에이전트 연결");

    await page.getByRole("button", { name: /복사/ }).first().click();
    await expect
      .poll(() => page.evaluate(() => (window as unknown as { __copied: string[] }).__copied.at(-1)))
      .toBe(MCP_URL);

    // 펼침 안의 Claude Code 명령과 claude.ai 안내.
    const more = page.locator("details summary").first();
    if (await more.count()) await more.click();
    // §15 N1: 모든 프로젝트에서 쓰이게 사용자 범위(-s user)로 더해요.
    await expect(page.getByText(`claude mcp add -s user --transport http jari ${MCP_URL}`)).toBeVisible();
    await expect(page.locator("main.screen")).toContainText(ASKS_FIRST);
    await expect(page.getByText("설정 → 커넥터 → 커스텀 커넥터 추가")).toBeVisible();
    await expectCleanLayout(page, "에이전트 연결(연결 방법 펼침)");
  });

  test("연결한 에이전트가 없으면 빈 상태를 보여요 @layout", async ({ signedIn: page }) => {
    await openAgents(page);
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toBeVisible();
    await expectCleanLayout(page, "에이전트 연결(빈 상태)");
  });

  test("코드는 대문자로 바뀌고 4자 뒤에 하이픈이 붙어요", async ({ signedIn: page }) => {
    await openAgents(page);
    await codeInput(page).pressSequentially("abcdefgh");
    await expect(codeInput(page)).toHaveValue("ABCD-EFGH");
  });

  test("틀린 코드는 입력칸 아래에 알려요 @layout", async ({ signedIn: page }) => {
    await openAgents(page);
    await enterCode(page, "zzzzzzzz");
    await expect(page.getByText("코드를 다시 확인해 주세요.")).toBeVisible();
    await expect(page.locator(".action-sheet")).toHaveCount(0);
    await expectCleanLayout(page, "에이전트 연결(틀린 코드)");
  });

  test("코드를 넣으면 확인 시트가 뜨고, 예약 허용은 꺼진 채로 연결해요 @layout", async ({ signedIn: page, user }) => {
    const pending = await pendingRequest("Claude");
    const requestId = await requestIdOf(user, pending.code);
    await openAgents(page);
    await enterCode(page, pending.code.replace("-", "").toLowerCase());

    const sheet = page.locator(".action-sheet");
    // §12 S-H1: 이름과 호스트는 확인된 값처럼 읽히지 않게 "밝힌 이름", "돌아갈 곳" 으로 보여요.
    await expect(sheet).toContainText("에이전트가 밝힌 이름: Claude");
    await expect(sheet).toContainText("돌아갈 곳: claude.ai");
    await expect(sheet).not.toContainText("요청한 곳");
    // §11 H1: 요청 시각과 원격 피싱 경고.
    await expect(sheet).toContainText(/(\d+분 전|방금) 요청/);
    await expect(sheet).toContainText(WARNING);
    await expect(sheet).toContainText("열차·좌석·상태 조회");
    // 시트에는 예약 허용 토글이 없어요. 새 연결은 늘 조회만 해요.
    await expect(sheet.getByRole("switch")).toHaveCount(0);
    await expect(sheet).not.toContainText("예약까지 맡기기");
    // 국가 헤더가 없는 로컬 요청이라 위치 줄도 없어요.
    await expect(sheet).not.toContainText("요청한 브라우저 위치");
    await expect(sheet.getByRole("button", { name: "거절" })).toBeVisible();
    await expectCleanLayout(page, "에이전트 연결 확인 시트");

    // 승인만 하고 토큰을 바꾸기 전에는 목록에 없어요. 브라우저 쪽이 끝나면 나와요.
    await approveAndExchange(page, user, pending, requestId);
    await expect(page.getByText("Claude").first()).toBeVisible();
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toHaveCount(0);
    await expectCleanLayout(page, "에이전트 연결(목록)");

    const [connection] = await connections(user);
    expect(connection).toMatchObject({ name: "Claude", allowBooking: false });
    await expect(page.getByRole("switch", { name: /예약까지 맡기기/ })).toHaveAttribute("aria-checked", "false");
  });

  test("다른 나라 브라우저가 만든 코드면 위치와 강한 경고를 보여요 @layout", async ({ signedIn: page }) => {
    const pending = await pendingRequest("Claude", CLAUDE, "JP");
    await phoneIn(page, "KR");
    await openAgents(page);
    await enterCode(page, pending.code);
    const sheet = page.locator(".action-sheet");
    await expect(sheet).toContainText(`요청한 브라우저 위치: ${countryName("JP")}`);
    await expect(sheet).toContainText(ELSEWHERE);
    await expectCleanLayout(page, "에이전트 연결 확인 시트(다른 나라)");
  });

  test("같은 나라면 위치만 보이고 불일치 경고는 없어요", async ({ signedIn: page }) => {
    const pending = await pendingRequest("Claude", CLAUDE, "KR");
    await phoneIn(page, "KR");
    await openAgents(page);
    await enterCode(page, pending.code);
    const sheet = page.locator(".action-sheet");
    await expect(sheet).toContainText(`요청한 브라우저 위치: ${countryName("KR")}`);
    await expect(sheet).not.toContainText(ELSEWHERE);
  });

  test("컴퓨터에서 실행한 프로그램은 시트와 목록에서 localhost 로 보여요 @layout", async ({ signedIn: page, user }) => {
    const pending = await pendingRequest("Claude Code", LOOPBACK);
    const requestId = await requestIdOf(user, pending.code);
    await openAgents(page);
    await enterCode(page, pending.code);
    const sheet = page.locator(".action-sheet");
    await expect(sheet).toContainText("돌아갈 곳: 컴퓨터에서 실행한 프로그램(localhost)");
    await expect(sheet).not.toContainText("이 컴퓨터의 프로그램");
    await expect(sheet).toContainText(WARNING);
    await expectCleanLayout(page, "에이전트 연결 확인 시트(로컬 프로그램)");

    await approveAndExchange(page, user, pending, requestId);
    await expect(page.getByText("컴퓨터에서 실행한 프로그램(localhost)").first()).toBeVisible();
    await expect(page.getByText("이 컴퓨터의 프로그램")).toHaveCount(0);
  });

  test("요청 시각은 코드를 받은 때부터 흘러요", async ({ signedIn: page }) => {
    const pending = await pendingRequest("Claude");
    // 시계는 signedIn 에서 지금으로 잡혀 있어요. 3분 당긴 뒤 코드를 넣어요.
    await page.clock.runFor(3 * 60_000);
    await openAgents(page);
    await enterCode(page, pending.code);
    await expect(page.locator(".action-sheet")).toContainText("3분 전 요청");
  });

  test("에이전트 이름은 글자 그대로만 보여요", async ({ signedIn: page }) => {
    const pending = await pendingRequest("<b>굵은</b> 에이전트");
    await openAgents(page);
    await enterCode(page, pending.code);
    const sheet = page.locator(".action-sheet");
    await expect(sheet).toContainText("<b>굵은</b> 에이전트");
    await expect(sheet.locator("b", { hasText: "굵은" })).toHaveCount(0);
  });

  test("브라우저 쪽이 끝나지 않으면 2분 뒤 기다림을 멈춰요 @layout", async ({ signedIn: page, user }) => {
    const pending = await pendingRequest("Claude");
    await openAgents(page);
    await enterCode(page, pending.code);
    await page.locator(".action-sheet").getByRole("button", { name: "연결하기" }).click();
    await expect(page.getByText(FINISHING)).toBeVisible();
    await expectCleanLayout(page, "에이전트 연결(브라우저 기다림)");

    let reads = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/mobile/agents") reads += 1;
    });
    // 2초마다 다시 읽어요. 한 번 돌릴 때마다 요청이 실제로 나갔는지 확인하고 다음으로 가요(느린 기기에서도).
    for (let round = 0; round < 2; round++) {
      const before = reads;
      await page.clock.runFor(2_000);
      await expect.poll(() => reads).toBeGreaterThan(before);
    }
    // 한도는 Date.now 기준이에요. 2분을 넘긴 뒤에도 다음 차례가 울릴 때까지 조금씩 돌려요.
    await page.clock.runFor(120_000);
    await advanceUntil(page, () => page.getByText(STALLED).isVisible());
    await expect(page.getByText(FINISHING)).toHaveCount(0);
    // §15 N2: 2분 안에 연결이 나타나지 않으면 다시 시작하라고 알려요.
    await expect(page.getByText(STALLED)).toBeVisible();
    await expectCleanLayout(page, "에이전트 연결(연결이 끝나지 않음)");
    const afterLimit = reads;
    await page.clock.runFor(10_000);
    // 울렸다면 나갔을 요청이 실제로 도착할 시간을 줘요.
    await page.waitForTimeout(1_000);
    expect(reads).toBe(afterLimit);
    expect(await connections(user)).toEqual([]);
    // 화면에 다시 들어오면 사라져요.
    await openAgents(page);
    await expect(page.getByText(STALLED)).toHaveCount(0);
  });

  test("승인하면 먼저 브라우저로 돌아가라고 하고, 연결이 나타나면 연결했다고 알려요", async ({ signedIn: page, user }) => {
    const pending = await pendingRequest("Claude");
    const requestId = await requestIdOf(user, pending.code);
    await openAgents(page);
    await enterCode(page, pending.code);
    await approveAndExchange(page, user, pending, requestId);
    await expect(page.getByText(STALLED)).toHaveCount(0);
  });

  test("같은 이름과 돌아갈 곳의 연결이 여럿이면 가장 최근 것에 표시해요 @layout", async ({ signedIn: page, user }) => {
    // §15 N3.
    for (let round = 0; round < 2; round++) {
      const pending = await pendingRequest("Claude");
      const requestId = await requestIdOf(user, pending.code);
      await openAgents(page);
      await enterCode(page, pending.code);
      await approveAndExchange(page, user, pending, requestId);
    }
    const listed = await connections(user);
    expect(listed).toHaveLength(2);
    await expect(page.getByText("가장 최근")).toHaveCount(1);
    await expectCleanLayout(page, "에이전트 연결(같은 이름 두 개)");
    // 가장 최근 표시는 나중에 연결한 줄에 있어요: 그 줄을 끊으면 표시도 사라져요.
    const newest = [...listed].sort((a, b) => b.createdAt.localeCompare(a.createdAt))[0]!;
    const response = await fetch(`${API}/api/mobile/agents/${encodeURIComponent(newest.id)}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${user.token}` },
    });
    expect(response.status).toBe(200);
    await openAgents(page);
    await expect(page.getByText("가장 최근")).toHaveCount(0);
  });

  test("연결 목록 한 줄의 연결 시각과 마지막 사용 시각은 같은 형식이에요", async ({ signedIn: page, user }) => {
    // §15 N4: 한 줄 안에서 하나는 날짜, 하나는 "N분 전"처럼 섞이지 않아요.
    const pending = await pendingRequest("Claude");
    const requestId = await requestIdOf(user, pending.code);
    await openAgents(page);
    await enterCode(page, pending.code);
    await page.locator(".action-sheet").getByRole("button", { name: "연결하기" }).click();
    await expect(page.locator(".toast")).toHaveText(APPROVED);
    const { access_token: token } = await finishInBrowser(pending, requestId);
    const used = await fetch(MCP_URL, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "ping" }),
    });
    expect(used.status).toBe(200);
    await openAgents(page);

    const line = page.getByText(/ 연결 · 마지막 사용 /).first();
    await expect(line).not.toContainText("아직 없음");
    const [connected, lastUsed] = (await line.innerText()).split(/ 연결 · 마지막 사용 /).map((part) => part.trim());
    const shape = (text: string) =>
      text
        .replace(/\d+/g, "0")
        .replace(/오전|오후/g, "때");
    expect(shape(lastUsed!)).toBe(shape(connected!));
  });

  test("연결이 없으면 연결 방법이 먼저 펼쳐져 있어요 @layout", async ({ signedIn: page }) => {
    // §15 N9: 연결이 없을 때는 지금 순서(연결 방법 → 코드 입력)를 지켜요.
    await openAgents(page);
    // 주소만 있는 요소(Claude Code 명령 줄이 아니라).
    const url = page.getByText(MCP_URL, { exact: true });
    await expect(url).toBeVisible();
    expect(await top(url)).toBeLessThan(await top(page.getByLabel("브라우저에 나온 8자리 코드")));
  });

  test("연결이 있으면 코드 입력, 연결 목록, 접힌 연결 방법 순서예요 @layout", async ({ signedIn: page, user }) => {
    // §15 N9.
    const pending = await pendingRequest("Claude");
    const requestId = await requestIdOf(user, pending.code);
    await openAgents(page);
    await enterCode(page, pending.code);
    await approveAndExchange(page, user, pending, requestId);
    await openAgents(page);

    const input = await top(page.getByLabel("브라우저에 나온 8자리 코드"));
    const row = await top(page.getByRole("button", { name: "연결 끊기" }));
    // 접힌 카드: 주소는 보이지 않고, 카드 자체(펼치는 줄)는 목록 아래에 있어요.
    await expect(page.getByText(MCP_URL, { exact: true })).toBeHidden();
    const card = page.locator("details", { has: page.getByText(MCP_URL, { exact: true }) });
    await expect(card).toHaveCount(1);
    expect(await card.evaluate((node) => (node as HTMLDetailsElement).open)).toBe(false);
    expect(input).toBeLessThan(row);
    expect(row).toBeLessThan(await top(card));
    await expectCleanLayout(page, "에이전트 연결(연결 있음, 카드 접힘)");

    await card.locator("summary").first().click();
    await expect(page.getByText(MCP_URL, { exact: true })).toBeVisible();
  });

  test("기다리는 중에 화면을 떠나면 목록을 다시 읽지 않아요", async ({ signedIn: page }) => {
    const pending = await pendingRequest("Claude");
    await openAgents(page);
    await enterCode(page, pending.code);
    await page.locator(".action-sheet").getByRole("button", { name: "연결하기" }).click();
    await expect(page.getByText(FINISHING)).toBeVisible();
    await page.locator(".bottom-nav [data-view='home']").click();
    let reads = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/mobile/agents") reads += 1;
    });
    await page.clock.runFor(10_000);
    await page.waitForTimeout(1_000);
    expect(reads).toBe(0);
  });

  test("거절하면 연결하지 않아요", async ({ signedIn: page, user }) => {
    const pending = await pendingRequest("Claude");
    await openAgents(page);
    await enterCode(page, pending.code);
    await page.locator(".action-sheet").getByRole("button", { name: "거절" }).click();
    await expect(page.locator(".toast")).toHaveText("연결을 거절했어요");
    await expect(page.locator(".action-sheet")).toHaveCount(0);
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toBeVisible();
    expect(await connections(user)).toEqual([]);
  });

  test("목록에서 예약 허용을 바꾸고 연결을 끊어요 @layout", async ({ signedIn: page, user }) => {
    const pending = await pendingRequest("Claude");
    const requestId = await requestIdOf(user, pending.code);
    await openAgents(page);
    await enterCode(page, pending.code);
    await approveAndExchange(page, user, pending, requestId);
    // §13 U2: 같은 이름의 연결을 구분하도록 연결한 날짜와 시:분을 보여요.
    // 앱의 formatStamp 와 같은 형식(ko-KR, 12시간제 "오후 03:04")이고, 앱처럼 기기(페이지)의 시간대를 써요.
    // 헤드리스는 timezoneId 가 Asia/Seoul, 기기는 그 기기의 시간대예요. 테스트를 돌리는 PC 의 시간대와는 상관없어요.
    const pageZone = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
    const hhmm = new Intl.DateTimeFormat("ko-KR", {
      timeZone: pageZone,
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
    const [connection] = await connections(user);
    await expect(page.locator("main.screen")).toContainText(hhmm.format(new Date(connection!.createdAt)));

    const toggle = page.getByRole("switch", { name: /예약까지 맡기기/ });
    await expect(toggle).toHaveAttribute("aria-checked", "false");
    await toggle.click();
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect.poll(async () => (await connections(user))[0]?.allowBooking).toBe(true);

    await page.getByRole("button", { name: "연결 끊기" }).click();
    const sheet = page.locator(".action-sheet");
    await expect(sheet.locator("[data-action='sheet-confirm']")).toHaveClass(/danger/);
    await expectCleanLayout(page, "연결 끊기 시트");
    await sheet.locator("[data-action='sheet-confirm']").click();
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toBeVisible();
    expect(await connections(user)).toEqual([]);
  });
});

// §11 L9: 체험 모드는 서버 없이 WXYZ-2345 로 흐름을 보여 줘요.
test.describe("체험 모드의 에이전트 연결", () => {
  test("입력칸 도움말의 데모 코드로 연결하고 목록에서 볼 수 있어요 @layout", async ({ app: page }) => {
    await openApp(page);
    await page.locator(".gate-card").nth(2).click();
    await expect(page.locator(".screen-home")).toBeVisible();
    await openAgents(page);
    await expect(page.getByText("WXYZ-2345").first()).toBeVisible();
    await expect(page.getByText("Claude").first()).toBeVisible();
    await expectCleanLayout(page, "에이전트 연결(체험 중)");

    await enterCode(page, "wxyz2345");
    await expect(page.locator(".action-sheet")).toContainText(WARNING);
    await expect(page.locator(".action-sheet").getByRole("switch")).toHaveCount(0);
    await page.locator(".action-sheet").getByRole("button", { name: "연결하기" }).click();
    // 데모는 브라우저 쪽이 없어 승인 즉시 목록에 나타나요. 마지막에 보이는 토스트는 연결했다는 말이에요.
    await expect(page.locator(".toast")).toHaveText(CONNECTED);
    await expect(page.getByRole("button", { name: "연결 끊기" })).toHaveCount(2);
  });
});

// §14 R1·R2: 구현 에이전트가 더한 두 건이에요.
test.describe("에이전트 연결 목록의 기준", () => {
  test("교환이 승인 직후에 끝나도 기다림 문구 없이 연결을 알려요", async ({ signedIn: page, user }) => {
    const pending = await pendingRequest("Claude");
    const requestId = await requestIdOf(user, pending.code);
    // 앱이 승인 응답을 받기 전에 브라우저 쪽 교환을 끝내요. 승인 뒤 목록에는 이미 새 연결이 있어요.
    // 기기 WebView 에서는 route.fetch 가 동작하지 않아요(Storage.getCookies 미지원). 승인은 테스트가 같은 사용자로
    // 직접 보내고, 브라우저 쪽 교환까지 마친 뒤 그 응답을 앱에 돌려줘요.
    await page.route(`${API}/api/mobile/agents/requests/approve`, async (route) => {
      const approved = await fetch(`${API}/api/mobile/agents/requests/approve`, {
        method: "POST",
        headers: { Authorization: `Bearer ${user.token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ requestId }),
      });
      const body = await approved.text();
      await finishInBrowser(pending, requestId);
      const origin = route.request().headers()["origin"];
      await route.fulfill({
        status: approved.status,
        contentType: "application/json",
        body,
        headers: origin ? { "Access-Control-Allow-Origin": origin, Vary: "Origin" } : {},
      });
    });
    await openAgents(page);
    await enterCode(page, pending.code);
    await page.locator(".action-sheet").getByRole("button", { name: "연결하기" }).click();
    await expect(page.locator(".toast")).toHaveText("연결했어요. 예약까지 맡기려면 아래 목록에서 켜 주세요.");
    await expect(page.getByText(FINISHING)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "연결 끊기" })).toHaveCount(1);
  });

  test("로그아웃하고 다른 계정으로 들어가면 앞 계정의 연결이 보이지 않아요", async ({ signedIn: page, user, control }) => {
    const pending = await pendingRequest("앞 계정 에이전트");
    const requestId = await requestIdOf(user, pending.code);
    const approved = await fetch(`${API}/api/mobile/agents/requests/approve`, {
      method: "POST",
      headers: { Authorization: `Bearer ${user.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ requestId }),
    });
    expect(approved.status).toBe(200);
    await finishInBrowser(pending, requestId);
    await openAgents(page);
    await expect(page.getByText("앞 계정 에이전트")).toBeVisible();

    // 페이지를 새로 읽지 않고 앱 안에서 로그아웃·로그인해요. 새로 읽으면 앞 상태가 저절로 사라져 확인이 안 돼요.
    await page.locator(".bottom-nav [data-view='settings']").click();
    await page.locator("[data-action='app-logout']").click();
    const sheetConfirm = page.locator("[data-action='sheet-confirm']");
    if (await sheetConfirm.isVisible().catch(() => false)) await sheetConfirm.click();
    await expect(page.locator(".auth-shell")).toBeVisible();
    const other = await control.newUser();
    await page.locator("[data-auth-gate='guest']").click();
    await page.locator("#auth-form [name='username']").fill(other.username);
    await page.locator("#auth-form [name='password']").fill(other.password);
    await page.locator("#auth-form [name='password']").press("Enter");
    await expect(page.locator(".screen-home")).toBeVisible();

    // 새 계정의 목록 응답을 붙잡아 두고, 그동안 앞 계정의 연결이 보이지 않는지 봐요.
    let release!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    await page.route(`${API}/api/mobile/agents`, async (route) => {
      await held;
      await route.continue();
    });
    await openAgents(page);
    await expect(page.getByText("연결 목록을 불러오고 있어요")).toBeVisible();
    await expect(page.getByText("앞 계정 에이전트")).toHaveCount(0);
    release();
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toBeVisible();
    await expect(page.getByText("앞 계정 에이전트")).toHaveCount(0);
  });
});
