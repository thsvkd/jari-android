import { expectCleanLayout } from "./layout";
import { expect, test } from "./fixtures";
import { E2E } from "../playwright.config";

const API = `http://127.0.0.1:${E2E.api}`;

test("공개 홈페이지 @layout", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name === "device", "공개 API 페이지는 Android WebView 화면이 아니에요.");

  const response = await page.goto(`${API}/`);
  expect(response?.status()).toBe(200);
  await expect(page.getByRole("heading", { name: "자리났다" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Android 앱 받기" })).toHaveAttribute(
    "href",
    "https://github.com/thsvkd/jari-android/releases/latest",
  );
  await expectCleanLayout(page, "공개 홈페이지");
});
