import { chromium, test as base, expect, type Page } from "@playwright/test";

import { E2E } from "../playwright.config";

const CONTROL = `http://127.0.0.1:${E2E.control}`;

export interface User {
  username: string;
  password: string;
  token: string;
}

export interface Scenario {
  login?: boolean;
  search?: "seats" | "sold_out" | "unavailable";
  seat_after_polls?: number;
  outcome?: "OUTSTANDING" | "PAID" | "RELEASED" | "UNKNOWN";
  deadline_minutes?: number;
  sold_seats?: Record<string, string[]> | null;
}

/** e2e 스택의 제어 서버. 가짜 코레일 시나리오와 호출 기록을 다뤄요. */
export class Control {
  async reset(): Promise<void> {
    await this.post("/reset", {});
  }

  async scenario(changes: Scenario): Promise<void> {
    await this.post("/scenario", changes);
  }

  async newUser(): Promise<User> {
    return this.post("/user", {});
  }

  /** 관리자가 만든 것과 같은 일회용 초대 코드예요. */
  async newInvite(): Promise<string> {
    return (await this.post<{ invite: string }>("/invite", {})).invite;
  }

  async korailLog(): Promise<Array<Record<string, unknown>>> {
    const reply = await fetch(`${CONTROL}/log`);
    return reply.json();
  }

  private async post<T>(path: string, body: unknown): Promise<T> {
    const reply = await fetch(`${CONTROL}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!reply.ok) throw new Error(`제어 서버 ${path} 실패: ${reply.status}`);
    return reply.json() as Promise<T>;
  }
}

/** 앱을 처음부터 열어요. 기기에서는 지난 테스트의 로그인이 Keystore 에 남아 있으니 먼저 로그아웃해요. */
export async function openApp(page: Page): Promise<void> {
  // 기기 WebView 에 CDP 로 붙은 페이지에는 baseURL 이 없어요. 이미 열린 앱 주소(https://localhost/)를 기준으로 해요.
  const current = page.url();
  await page.goto(current.startsWith("http") ? new URL("/", current).href : "/");
  // 앱을 막 띄운 기기의 첫 로딩(WebView 준비·Keystore 읽기)은 에뮬레이터와 느린 폰에서 10초를 넘기기도 해요.
  await expect(page.locator(".auth-shell, main.screen").first()).toBeVisible({ timeout: 30_000 });
  if (await page.locator(".auth-shell").count()) return;
  await page.locator(".bottom-nav [data-view='settings']").click();
  await page.locator("[data-action='app-logout']").click();
  const sheetConfirm = page.locator("[data-action='sheet-confirm']");
  if (await sheetConfirm.isVisible().catch(() => false)) await sheetConfirm.click();
  await expect(page.locator(".auth-shell")).toBeVisible();
}

/** 초대 회원 로그인 화면을 거쳐 홈까지 들어가요. 실제 사용자가 누르는 순서 그대로예요. */
export async function signIn(page: Page, user: User): Promise<void> {
  await openApp(page);
  await page.locator("[data-auth-gate='guest']").click();
  await page.locator("#auth-form [name='username']").fill(user.username);
  await page.locator("#auth-form [name='password']").fill(user.password);
  // 키보드의 이동(Enter)으로 제출해요. 폰에서는 키보드가 올라오는 동안 버튼 위치가 흔들려 누르기를 기다리다 멈출 수 있어요.
  await page.locator("#auth-form [name='password']").press("Enter");
  await expect(page.locator(".screen-home")).toBeVisible();
}

/**
 * 앱은 30초마다 상태를 다시 읽어요. 시계를 당겨 그 폴링을 바로 일으키고, 다시 실제 시각으로 맞춰요.
 * 앱 시계만 앞서 가면 서버의 마지막 조회가 오래전 일로 보여 '멈춤'으로 바뀌어요. 그건 테스트가 만든 상태예요.
 */
export async function nextPoll(page: Page): Promise<void> {
  await page.clock.runFor(31_000);
  await page.clock.setSystemTime(new Date());
}

export const test = base.extend<{ control: Control; user: User; app: Page; signedIn: Page }>({
  // 헤드리스에서는 Playwright 의 새 페이지, 기기에서는 e2e 앱의 WebView 에 CDP 로 붙은 페이지예요.
  app: async ({ page }, use, testInfo) => {
    if (testInfo.project.name !== "device") return use(page);
    const endpoint = process.env.JARI_DEVICE_CDP;
    if (!endpoint) throw new Error("JARI_DEVICE_CDP 가 없어요. scripts/verify.mjs 가 기기를 준비한 뒤 실행해요.");
    // noDefaults: 붙은 WebView 에 Playwright 기본 설정(다운로드·색상 에뮬레이션 등)을 걸지 않아요.
    // 구글 WebView(에뮬레이터)는 다운로드 설정 명령(Browser.setDownloadBehavior)을 거부해 연결부터 실패해요.
    const browser = await chromium.connectOverCDP(endpoint, { noDefaults: true });
    const webview = browser.contexts()[0]?.pages()[0];
    if (!webview) throw new Error("기기의 e2e 앱 WebView 를 찾지 못했어요.");
    await use(webview);
    await browser.close();
  },
  control: async ({}, use) => {
    const control = new Control();
    await control.reset();
    await use(control);
  },
  user: async ({ control }, use) => {
    await use(await control.newUser());
  },
  signedIn: async ({ app: page, user }, use) => {
    // 폴링·카운트다운을 테스트가 움직일 수 있게 시계를 먼저 잡아요. 시작 시각은 지금이에요.
    await page.clock.install({ time: new Date() });
    await signIn(page, user);
    await use(page);
  },
});

export { expect };
