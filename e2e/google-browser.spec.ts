import { execFileSync } from "node:child_process";
import path from "node:path";

import { expect, test } from "./fixtures";
import { expectCleanLayout } from "./layout";

interface GooglePages {
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

test("외부 브라우저의 Google 연결 확인·오류·완료·거부 화면이 읽고 누르기 편해요 @layout", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "device", "Google 확인 페이지는 앱 WebView 밖의 외부 브라우저 화면이에요.");
  const pages = renderGooglePages();

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
