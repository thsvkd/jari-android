import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, test } from "./fixtures";
import { expectCleanLayout } from "./layout";

interface GooglePages {
  appReturn: string;
  appMissing: string;
  confirmation: string;
  error: string;
  success: string;
  denied: string;
}

function renderGooglePages(): GooglePages {
  const output = execFileSync(
    "uv",
    ["run", "--frozen", "python", "tests/e2e/google_pages.py"],
    { cwd: path.join(process.cwd(), "backend"), encoding: "utf8" },
  );
  return JSON.parse(output) as GooglePages;
}

test("외부 브라우저의 Google 앱 복귀·연결 확인·오류·완료·거부 화면이 읽고 누르기 편해요 @layout", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "device", "Google 확인 페이지는 앱 WebView 밖의 외부 브라우저 화면이에요.");
  const pages = renderGooglePages();

  // 안드로이드 앱이 시작한 흐름: 코드 없이, 이 휴대폰의 앱으로 돌아가는 버튼만 있어요.
  // 페이지가 스스로 앱을 여는 meta refresh 는 테스트 브라우저가 따라가지 않게 빼고 그려요.
  await page.setContent(pages.appReturn.replace(/<meta http-equiv="refresh"[^>]*>/, ""));
  await expect(page.getByRole("heading", { name: "alice 앱 계정에 Google 연결을 마무리해요" })).toBeVisible();
  const openApp = page.getByRole("link", { name: "자리났다 앱 열기" });
  await expect(openApp).toHaveAttribute("href", /^intent:\/\/google-auth\?handoff=[\w-]+#Intent;scheme=dev\.thsvkd\.jari;package=dev\.thsvkd\.jari;/);
  await expect(page.locator("form, input")).toHaveCount(0);
  await expectCleanLayout(page, "Google 외부 브라우저 앱 복귀");

  await page.setContent(pages.appMissing);
  await expect(page.getByRole("heading", { name: "자리났다 앱을 열 수 없어요" })).toBeVisible();
  await expectCleanLayout(page, "Google 외부 브라우저 앱 없음");

  await page.setContent(pages.confirmation);
  await expect(page.getByRole("heading", { name: "alice 앱 계정에 Google 연결을 허용할까요?" })).toBeVisible();
  await expect(page.locator("[name=verificationCode]")).toHaveAttribute("autocomplete", "one-time-code");
  await expect(page.getByRole("button", { name: "이 앱의 로그인 허용" })).toBeVisible();
  await expect(page.getByRole("button", { name: "거부" })).toBeVisible();
  await expectCleanLayout(page, "Google 외부 브라우저 확인");

  await page.setContent(pages.error);
  await expect(page.getByText("앱에 표시된 확인 코드가 맞지 않아요.")).toBeVisible();
  await expect(page.locator("[name=verificationCode]")).toBeVisible();
  await expectCleanLayout(page, "Google 외부 브라우저 코드 오류");

  await page.setContent(pages.success);
  await expect(page.getByText("확인이 끝났어요. 앱으로 돌아가 완료해 주세요.")).toBeVisible();
  await expect(page.locator("form")).toHaveCount(0);
  await expectCleanLayout(page, "Google 외부 브라우저 완료");

  await page.setContent(pages.denied);
  await expect(page.getByText("이 앱의 로그인 요청을 거부했어요. 창을 닫아도 돼요.")).toBeVisible();
  await expect(page.locator("form")).toHaveCount(0);
  await expectCleanLayout(page, "Google 외부 브라우저 거부");
});
