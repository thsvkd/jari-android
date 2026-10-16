import { createHash, randomBytes } from "node:crypto";

import type { Page } from "@playwright/test";

import { E2E } from "../playwright.config";
import { expect, openApp, test, type User } from "./fixtures";
import { expectCleanLayout } from "./layout";

// 에이전트 연결(명세 .omc/plans/mcp-agent-spec.md §7). 연결은 실제 MCP 클라이언트와 브라우저처럼
// /oauth/register → /oauth/authorize(로그인 폼) → 폼 POST → /oauth/token 으로 만들어요.
// 스택은 MOBILE_PUBLIC_URL 을 API 주소로 띄워요.
const API = `http://127.0.0.1:${E2E.api}`;
const MCP_URL = `${API}/mcp`;
const CLAUDE = "https://claude.ai/api/mcp/auth_callback";
const LOOPBACK = "http://127.0.0.1:53682/callback";
// §15 N7: 에이전트가 확인을 받는다는 말은 서버가 강제하지 않으니 낮춰서 말해요.
const HIDDEN = /<input type="hidden" name="([a-z_]+)" value="([^"]*)">/g;

const unescapeHtml = (text: string) =>
  text.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

/**
 * 에이전트가 브라우저를 열고, 사용자가 자리났다 아이디·비밀번호로 로그인해 허용하고, 에이전트가 코드를 토큰으로 바꿔요.
 * 교환이 끝나야 연결이 목록에 나와요(§11 L4).
 */
async function connectInBrowser(user: User, name = "e2e 에이전트", redirect = CLAUDE): Promise<{ access_token: string }> {
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
  const page = await fetch(`${API}/oauth/authorize?${query}`);
  expect(page.status).toBe(200);
  const fields = Object.fromEntries([...(await page.text()).matchAll(HIDDEN)].map(([, key, value]) => [key, unescapeHtml(value!)]));
  const cookie = (page.headers.getSetCookie().find((header) => header.startsWith("jari_oauth=")) ?? "").split(";")[0]!;
  const allowed = await fetch(`${API}/oauth/authorize`, {
    method: "POST",
    redirect: "manual",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Cookie: cookie },
    body: new URLSearchParams({ ...fields, username: user.username, password: user.password, decision: "allow" }),
  });
  expect(allowed.status).toBe(302);
  const code = new URL(allowed.headers.get("location")!).searchParams.get("code")!;
  const exchanged = await fetch(`${API}/oauth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirect,
      client_id: clientId,
      code_verifier: verifier,
      resource: MCP_URL,
    }),
  });
  expect(exchanged.status).toBe(200);
  return (await exchanged.json()) as { access_token: string };
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

async function openAgents(page: Page): Promise<void> {
  await page.locator(".bottom-nav [data-view='settings']").click();
  const row = page.getByRole("button", { name: /에이전트 연결/ });
  await expect(row).toContainText("AI 에이전트가 열차를 찾고 예약하게 해요");
  await row.click();
}

test.describe("에이전트 연결", () => {
  test("연결 방법에는 하네스별 명령과 복사 버튼만 있어요 @layout", async ({ signedIn: page }) => {
    await openAgents(page);
    await page.evaluate(() => {
      const copied: string[] = [];
      (window as unknown as { __copied: string[] }).__copied = copied;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true, value: { writeText: async (text: string) => void copied.push(text) },
      });
    });
    const commands = [
      ["Claude Code", `claude mcp add -s user --transport http jari ${MCP_URL}`],
      ["Codex", `codex mcp add jari --url ${MCP_URL}\ncodex mcp login jari`],
    ];
    for (const [name, command] of commands) {
      await page.getByRole("button", { name: `${name} 명령 복사`, exact: true }).click();
      await expect.poll(() => page.evaluate(() => (window as unknown as { __copied: string[] }).__copied.at(-1))).toBe(command);
    }
    await expect(page.locator("[data-agent-harness]")).toHaveCount(2);
    await expect(page.locator(".agent-note, .agent-howto")).toHaveCount(0);
    await expectCleanLayout(page, "에이전트 연결 명령");
  });

  test("연결한 에이전트가 없으면 빈 상태를 보여요 @layout", async ({ signedIn: page }) => {
    await openAgents(page);
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toBeVisible();
    await expectCleanLayout(page, "에이전트 연결(빈 상태)");
  });

  test("코드 입력 없이 브라우저 로그인으로 연결한다고 안내해요", async ({ signedIn: page }) => {
    await openAgents(page);
    await expect(page.locator("main.screen form")).toHaveCount(0);
    await expect(page.locator("main.screen")).not.toContainText("코드");
  });

  test("브라우저에서 연결하고 앱으로 돌아오면 목록에 조회만 하는 연결로 보여요 @layout", async ({ signedIn: page, user }) => {
    await openAgents(page);
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toBeVisible();
    await connectInBrowser(user, "Claude");
    // 브라우저에 다녀온 앱은 다시 보이게 되면서 목록을 새로 읽어요.
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(page.getByRole("button", { name: "연결 끊기" })).toHaveCount(1);
    await expect(page.getByText("claude.ai").first()).toBeVisible();
    await expect(page.getByText("아직 연결한 에이전트가 없어요")).toHaveCount(0);
    await expect(page.getByRole("switch", { name: /예약까지 맡기기/ })).toHaveAttribute("aria-checked", "false");
    await expectCleanLayout(page, "에이전트 연결(목록)");
    const [connection] = await connections(user);
    expect(connection).toMatchObject({ name: "Claude", allowBooking: false });
  });

  test("컴퓨터에서 실행한 프로그램은 목록에서 localhost 로 보여요 @layout", async ({ signedIn: page, user }) => {
    await connectInBrowser(user, "Claude Code", LOOPBACK);
    await openAgents(page);
    await expect(page.getByText("컴퓨터에서 실행한 프로그램(localhost)").first()).toBeVisible();
    await expect(page.getByText("이 컴퓨터의 프로그램")).toHaveCount(0);
    await expectCleanLayout(page, "에이전트 연결(로컬 프로그램)");
  });

  test("새 연결이 생기면 알림 화면에 에이전트 알림이 와요 @layout", async ({ signedIn: page, user }) => {
    // 남이 보낸 인가 링크에 로그인했어도(피싱) 앱 알림으로 바로 알고 끊을 수 있어요.
    await connectInBrowser(user, "Claude");
    await page.locator("[data-view='notifications']").first().click();
    const notice = page.locator(".notification", { hasText: "새 에이전트가 연결됐어요" });
    await expect(notice).toHaveCount(1);
    await expect(notice.locator(".notification-icon")).toHaveAttribute("aria-label", "에이전트");
    await expect(notice.locator(".notification-head b")).toHaveText("에이전트 연결");
    await expect(notice.locator(".notification-text")).toContainText("Claude (claude.ai)");
    await expectCleanLayout(page, "알림(새 에이전트 연결)");
  });

  test("에이전트 이름은 글자 그대로만 보여요", async ({ signedIn: page, user }) => {
    await connectInBrowser(user, "<b>굵은</b> 에이전트");
    await openAgents(page);
    const list = page.locator(".agent-list");
    // 이름은 앱이 그린 굵은 글씨 안에 글자 그대로 들어가요. 이름의 태그가 요소가 되지 않아요.
    await expect(list.locator(".agent-row-head > b")).toHaveText("<b>굵은</b> 에이전트");
    await expect(list.locator(".agent-row-head b b")).toHaveCount(0);
  });

  test("같은 이름과 돌아갈 곳의 연결이 여럿이면 가장 최근 것에 표시해요 @layout", async ({ signedIn: page, user }) => {
    // §15 N3.
    await connectInBrowser(user, "Claude");
    await connectInBrowser(user, "Claude");
    await openAgents(page);
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
    const { access_token: token } = await connectInBrowser(user, "Claude");
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

  test("연결이 없으면 하네스별 명령이 바로 보여요 @layout", async ({ signedIn: page }) => {
    await openAgents(page);
    await expect(page.locator("[data-agent-harness='claude'] pre")).toContainText(MCP_URL);
    await expect(page.locator("[data-agent-harness='codex'] pre")).toContainText("codex mcp login jari");
  });

  test("연결이 있어도 하네스별 명령과 연결 목록을 보여요 @layout", async ({ signedIn: page, user }) => {
    await connectInBrowser(user, "Claude");
    await openAgents(page);
    await expect(page.getByRole("button", { name: "연결 끊기" })).toBeVisible();
    await expect(page.locator("[data-agent-harness]")).toHaveCount(2);
    await expectCleanLayout(page, "에이전트 연결(연결 있음)");
  });

  test("목록에서 예약 허용을 바꾸고 연결을 끊어요 @layout", async ({ signedIn: page, user }) => {
    await connectInBrowser(user, "Claude");
    await openAgents(page);
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

// 체험 모드는 서버 없이 연결 목록을 보여 줘요. 브라우저 로그인이 없어 처음부터 연결 하나가 있어요.
test.describe("체험 모드의 에이전트 연결", () => {
  test("안내와 데모 연결 하나를 보여요 @layout", async ({ app: page }) => {
    await openApp(page);
    await page.locator(".gate-card").nth(2).click();
    await expect(page.locator(".screen-home")).toBeVisible();
    await openAgents(page);
    await expect(page.getByText("Claude").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "연결 끊기" })).toHaveCount(1);
    await expectCleanLayout(page, "에이전트 연결(체험 중)");
    // 체험 알림에도 새 연결 알림이 하나 있어요.
    await page.locator("[data-view='notifications']").first().click();
    await expect(page.locator(".notification-icon[aria-label='에이전트']")).toHaveCount(1);
    await expectCleanLayout(page, "알림(체험 중)");
  });
});

test.describe("에이전트 연결 목록의 기준", () => {
  test("로그아웃하고 다른 계정으로 들어가면 앞 계정의 연결이 보이지 않아요", async ({ signedIn: page, user, control }) => {
    await connectInBrowser(user, "앞 계정 에이전트");
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
