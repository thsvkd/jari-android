import type { Page } from "@playwright/test";

import { E2E } from "../playwright.config";
import { expect, openApp, test, type Control, type User } from "./fixtures";
import { expectCleanLayout } from "./layout";

const API = `http://127.0.0.1:${E2E.api}/api/mobile`;
const PASSWORD = "a long secure passphrase";

type Applicant = User & { id: string };

async function request(path: string, token: string, body?: unknown): Promise<Response> {
  return fetch(`${API}${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
}

async function applicant(): Promise<Applicant> {
  const username = `apply_${Math.random().toString(36).slice(2, 12)}`;
  const reply = await fetch(`${API}/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password: PASSWORD, applicationNote: "주말에 부산에 가는 지인이에요." }),
  });
  expect(reply.status).toBe(200);
  const auth = await reply.json() as { token: string; user: { id: string; status: string } };
  expect(auth.user.status).toBe("pending");
  return { username, password: PASSWORD, token: auth.token, id: auth.user.id };
}

async function login(page: Page, user: User, admin = false): Promise<void> {
  await openApp(page);
  await page.locator(`[data-auth-gate='${admin ? "admin" : "guest"}']`).click();
  await page.locator("#auth-form [name='username']").fill(user.username);
  await page.locator("#auth-form [name='password']").fill(user.password);
  await page.locator("#auth-form [name='password']").press("Enter");
}

async function decide(control: Control, user: Applicant, status: "approved" | "rejected"): Promise<void> {
  const admin = await control.adminUser();
  const reply = await request(`/admin/users/${user.id}/approval`, admin.token, { status });
  expect(reply.status).toBe(200);
}

test.describe("회원 승인", () => {
  test("가입 뒤 승인 상태를 확인하고 승인되면 같은 로그인으로 이용해요 @layout", async ({ app: page, control }, testInfo) => {
    await openApp(page);
    await page.locator("[data-auth-gate='guest']").click();
    await page.locator("[data-auth-mode='register']").click();
    const username = `new_${Math.random().toString(36).slice(2, 12)}`;
    const form = page.locator("#auth-form");
    await form.locator("[name='username']").fill(username);
    await form.locator("[name='password']").fill(PASSWORD);
    await form.locator("[name='password_confirm']").fill(PASSWORD);
    await form.locator("[name='applicationNote']").fill("함께 여행하는 지인이에요.");
    const registered = page.waitForResponse((reply) => reply.url().endsWith("/auth/register") && reply.request().method() === "POST");
    await form.locator("[name='password_confirm']").press("Enter");
    const auth = await (await registered).json() as { token: string; user: { id: string; status: string } };
    expect(auth.user.status).toBe("pending");
    const user = { username, password: PASSWORD, token: auth.token, id: auth.user.id };
    await expect(page.locator(".screen-approval")).toContainText(username);
    await expectCleanLayout(page, "신규 가입 후 승인 대기");
    // Android는 Keystore에서 복원하고, 브라우저는 보안상 토큰을 프로세스 메모리에만 둬요.
    // 헤드리스에서는 페이지를 재로딩하지 않고 같은 앱 세션의 상태 갱신을 검증해요.
    if (testInfo.project.name === "device") await page.reload();
    else await page.locator("[data-action='refresh-approval']").click();
    await expect(page.locator(".screen-approval")).toBeVisible();
    await expect(page.locator(".bottom-nav")).toHaveCount(0);
    const before = (await control.korailLog()).length;
    expect((await request("/trains", user.token, { conditions: {} })).status).toBe(403);
    await page.locator("[data-action='refresh-approval']").click();
    await expect(page.locator(".screen-approval")).toBeVisible();
    expect((await control.korailLog()).length).toBe(before);
    await decide(control, user, "approved");
    await page.locator("[data-action='refresh-approval']").click();
    await expect(page.locator(".screen-home")).toBeVisible();
    const state = await request("/bootstrap", user.token, {}).then((reply) => reply.json()) as { user: { status: string } };
    expect(state.user.status).toBe("approved");
    await expectCleanLayout(page, "승인 직후 코레일 연결 전 홈");
  });

  test("관리자가 대기 목록에서 메모를 확인하고 승인해요 @layout", async ({ app: page, control }) => {
    const user = await applicant();
    const rejected = await applicant();
    const admin = await control.adminUser();
    await login(page, admin, true);
    await expect(page.locator(".screen-home")).toBeVisible();
    await page.locator(".bottom-nav [data-view='settings']").click();
    await page.locator("[data-view='members']").click();
    const row = page.locator(`[data-member-id='${user.id}']`);
    await expect(row).toContainText(user.username);
    await expect(row).toContainText("주말에 부산에 가는 지인이에요.");
    await expectCleanLayout(page, "관리자 회원 승인 목록");
    await row.locator("[data-action='approve-member']").click();
    await expect(row).toContainText("승인");
    await expect(row.locator("[data-action='reject-member']")).toHaveCount(0);
    const state = await request("/bootstrap", user.token, {}).then((reply) => reply.json()) as { user: { status: string } };
    expect(state.user.status).toBe("approved");
    await expectCleanLayout(page, "관리자 회원 승인 완료");
    const rejectedRow = page.locator(`[data-member-id='${rejected.id}']`);
    await rejectedRow.locator("[data-action='reject-member']").click();
    await expect(rejectedRow).toContainText("거절");
    const denied = await request("/bootstrap", rejected.token, {}).then((reply) => reply.json()) as { user: { status: string } };
    expect(denied.user.status).toBe("rejected");
    await expectCleanLayout(page, "관리자 회원 승인 거절 완료");
  });

  test("거절 상태를 보여주고 대기·거절 사용자도 로그아웃하거나 탈퇴할 수 있어요 @layout", async ({ app: page, control }) => {
    const user = await applicant();
    await login(page, user);
    await expect(page.locator(".screen-approval")).toBeVisible();
    await page.locator("[data-action='app-logout']").click();
    await expect(page.locator(".auth-shell")).toBeVisible();
    await decide(control, user, "rejected");
    await login(page, user);
    await expect(page.locator(".screen-approval")).toContainText("승인되지 않았어요");
    await expectCleanLayout(page, "승인 신청 거절");
    expect((await request("/status", user.token)).status).toBe(403);
    await page.locator("[data-action='delete-account']").click();
    await expect(page.locator(".action-sheet")).toBeVisible();
    await expectCleanLayout(page, "승인 전 회원 탈퇴 확인");
    await page.locator("#sheet-password").fill(user.password);
    await page.locator("[data-action='sheet-confirm']").click();
    await expect(page.locator(".auth-shell")).toContainText("탈퇴했어요");
  });

  test("회원 목록 조회 실패를 알리고 다시 불러올 수 있어요 @layout", async ({ app: page, control }) => {
    const user = await applicant();
    const admin = await control.adminUser();
    await login(page, admin, true);
    await expect(page.locator(".screen-home")).toBeVisible();
    await page.locator(".bottom-nav [data-view='settings']").click();
    await page.locator("[data-view='members']").click();
    await expect(page.locator(`[data-member-id='${user.id}']`)).toContainText(user.username);
    await page.locator(".screen-members [data-action='back']").click();
    await page.route("**/api/mobile/admin/users", (route) => route.fulfill({ status: 503, json: { error: "회원 목록을 불러올 수 없어요." } }));
    await page.locator("[data-view='members']").click();
    await expect(page.locator(".screen-members")).toContainText("회원 목록을 불러올 수 없어요.");
    await expect(page.locator("[data-action='approve-member']")).toHaveCount(0);
    await expectCleanLayout(page, "회원 목록 조회 실패");
    await page.unroute("**/api/mobile/admin/users");
    await page.locator(".screen-members").getByRole("button", { name: /다시|새로/ }).click();
    await expect(page.locator(`[data-member-id='${user.id}']`)).toContainText(user.username);
    await expectCleanLayout(page, "회원 목록 조회 재시도");
  });

  test("승인 상태 조회 실패를 알리고 대기 화면에서 다시 확인할 수 있어요 @layout", async ({ app: page, control }) => {
    await control.reset();
    const user = await applicant();
    await login(page, user);
    await expect(page.locator(".screen-approval")).toBeVisible();
    await page.route("**/api/mobile/bootstrap", (route) => route.fulfill({ status: 503, json: { error: "승인 상태를 확인할 수 없어요." } }));
    await page.locator("[data-action='refresh-approval']").click();
    await expect(page.locator(".screen-approval")).toContainText("승인 상태를 확인할 수 없어요.");
    await expect(page.locator(".blocker")).toHaveCount(0);
    await expectCleanLayout(page, "승인 상태 조회 실패");
    await page.unroute("**/api/mobile/bootstrap");
    await page.locator("[data-action='refresh-approval']").click();
    await expect(page.locator(".screen-approval")).not.toContainText("승인 상태를 확인할 수 없어요.");
    await expect(page.locator("[data-action='app-logout']")).toBeEnabled();
    await expectCleanLayout(page, "승인 상태 조회 재시도");
  });
});
