#!/usr/bin/env node
// Play 스토어와 README 에 쓰는 앱 스크린숏을 데모 모드 앱에서 찍어요. 화면이 바뀌면 다시 돌려요.
//
//   node scripts/render-screenshots.mjs
//
// 만드는 것: store/screenshots/{1-home,2-activity,3-trains,4-seatmap}.png (1080×2160, 라이트 테마)
//
// 데모 개발 서버를 잠깐 띄우고 Playwright(Chromium)로 사용자처럼 눌러 가며 찍어요. 서버·코레일에는 닿지 않아요.
// 시계는 오늘 오전 10:30(한국 시간)으로 고정해요. 찍은 시각(새벽 3시에 "시작" 같은)이 화면에 남지 않게 해요.
// 글꼴은 이 PC 의 한글 글꼴로 그려요. Linux 에서는 CI 처럼 fonts-noto-cjk 를 깔면 Android 기기와 같은 글꼴이 돼요.

import { chromium } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const OUT = join(ROOT, "store/screenshots");
// 개발 서버(4173)·미리보기(4174)·e2e(4273)와 겹치지 않는 포트예요.
const PORT = 4276;
const URL = `http://127.0.0.1:${PORT}/`;
// 400×800 을 2.7배로 그려 Play 가 받는 1080×2160(1:2)이 돼요.
const PHONE = {
  viewport: { width: 400, height: 800 },
  deviceScaleFactor: 2.7,
  isMobile: true,
  hasTouch: true,
  locale: "ko-KR",
  timezoneId: "Asia/Seoul",
  colorScheme: "light",
};
const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Seoul" }).format(new Date());
const NOW = new Date(`${today}T10:30:00+09:00`);

async function waitForServer(server) {
  for (let attempt = 0; attempt < 120; attempt++) {
    if (server.exitCode !== null) throw new Error(`데모 서버가 멈췄어요(종료 코드 ${server.exitCode}).`);
    try {
      if ((await fetch(URL)).ok) return;
    } catch {
      // 아직 뜨는 중이에요.
    }
    await new Promise((done) => setTimeout(done, 500));
  }
  throw new Error(`데모 서버가 ${URL} 에서 답하지 않아요.`);
}

async function shoot(page, name) {
  // 글꼴을 다 받고, 화면 전환 애니메이션이 끝난 뒤에 찍어요.
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(600);
  const file = join(OUT, `${name}.png`);
  await page.screenshot({ path: file });
  console.log(`  ${file}`);
}

// e2e/flows.ts 의 pickTime 과 같은 방법이에요: 접힌 시간 칸을 펼쳐 시·분 슬라이더에 값을 넣고 다시 접어요.
async function pickTime(page, name, clock) {
  const picker = page.locator("[data-time-picker]", { has: page.locator(`[name=${name}]`) });
  const [hour, minute] = clock.split(":");
  const toggle = picker.locator("[data-tp='toggle']");
  await toggle.evaluate((element) => element.scrollIntoView({ block: "center" }));
  if ((await toggle.getAttribute("aria-expanded")) !== "true") await toggle.click();
  await picker.locator("[data-tp-slider='hour']").fill(String(Number(hour)));
  await picker.locator("[data-tp-slider='minute']").fill(String(Number(minute)));
  await toggle.click();
  if ((await page.locator(`[name=${name}]`).inputValue()) !== clock) throw new Error(`${name} 을 ${clock} 로 고르지 못했어요.`);
}

mkdirSync(OUT, { recursive: true });
const server = spawn(process.execPath, [join(ROOT, "node_modules/vite/bin/vite.js"), "--mode", "demo", "--port", String(PORT), "--strictPort"], {
  cwd: ROOT,
  stdio: "ignore",
});
const browser = await chromium.launch();
try {
  await waitForServer(server);
  const page = await browser.newPage(PHONE);
  await page.clock.setFixedTime(NOW);
  await page.goto(URL);
  await page.locator(".screen-home").waitFor();

  // 1. 홈: 찾는 중인 자리 찾기 카드와 바로가기
  await shoot(page, "1-home");

  // 2. 내 예약: 진행 중인 자리 찾기의 조건과 상태
  await page.locator(".bottom-nav [data-view='activity']").click();
  await page.locator(".screen-activity").waitFor();
  await shoot(page, "2-activity");

  // 3. 열차 목록: 한 편을 좌석 무관으로 고른 상태
  await page.locator(".bottom-nav [data-view='home']").click();
  await page.locator(".screen-home [data-action='new-journey']").click();
  await page.locator(".screen-journey").waitFor();
  await pickTime(page, "dep_time", "07:00");
  if (!(await page.locator("[name=unlimited_time]").isChecked())) {
    await page.locator("label", { has: page.locator("[name=unlimited_time]") }).click();
  }
  await page.getByRole("button", { name: /열차 조회하기/ }).click();
  await page.locator(".screen-trains [data-train-toggle]").first().waitFor();
  await page.locator("[data-train-toggle]").nth(1).click();
  // 하단 "다음" 버튼은 떠 있고 탭 바는 반투명이라, 맨 위에서 찍으면 목록 끝 카드가 둘 사이로 잘려 비쳐요.
  // 안내 문장이 머리말 바로 아래에 오도록 내려 마지막 카드가 버튼 위에서 끝나게 찍어요.
  const lastCardBottom = await page.evaluate(() => {
    const intro = document.querySelector(".screen-trains .intro");
    const topbar = document.querySelector(".topbar");
    window.scrollTo(0, intro.getBoundingClientRect().top + window.scrollY - topbar.offsetHeight);
    const cards = document.querySelectorAll(".train-list .train-card");
    return cards[cards.length - 1].getBoundingClientRect().bottom;
  });
  const actionTop = await page.locator("[data-action='trains-next']").evaluate((element) => element.getBoundingClientRect().top);
  if (lastCardBottom > actionTop) {
    throw new Error(`열차 목록의 마지막 카드(${lastCardBottom}px)가 "다음" 버튼(${actionTop}px)에 가려요. 내리는 위치를 고쳐 주세요.`);
  }
  await shoot(page, "3-trains");

  // 4. 좌석표: 매진 열차에서 취소표를 기다릴 좌석 둘을 고른 상태
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator("[data-seat-map]").first().click();
  await page.locator(".seat-cell").first().waitFor();
  const waitable = page.locator(".seat-cell.occupied:not([disabled])", { hasText: "대기" });
  await waitable.nth(0).click();
  await waitable.nth(1).click();
  await page.locator("[data-action='confirm-seat-dialog']:not([disabled])").waitFor();
  await shoot(page, "4-seatmap");
} finally {
  await browser.close();
  server.kill();
}
