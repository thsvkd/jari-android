import { defineConfig } from "@playwright/test";

// e2e 스택(실서버 + 가짜 코레일)과 그 서버를 바라보는 웹 빌드를 띄워 놓고, 사용자가 보는 화면을 끝까지 따라가요.
// 실제 코레일에는 닿지 않아요. 포트는 개발용 스택(18081·18090)과 겹치지 않게 따로 둬요.
export const E2E = {
  api: 18281,
  control: 18290,
  redis: 16579,
  web: 4273,
} as const;

const phone = {
  viewport: { width: 412, height: 915 },
  deviceScaleFactor: 2.625,
  isMobile: true,
  hasTouch: true,
  locale: "ko-KR",
  timezoneId: "Asia/Seoul",
};

export default defineConfig({
  testDir: "e2e",
  outputDir: "test-results/e2e",
  // 한 스택을 여러 테스트가 같이 써요. 사용자는 테스트마다 새로 만들지만, 가짜 코레일 시나리오는 하나라 차례로 돌려요.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  // 스크린샷 기준 이미지는 글꼴이 번들되지 않아 OS 마다 따로예요. 지금은 로컬(Windows) 기준만 있어서 CI(Linux)는 비교를 빼요.
  grepInvert: process.env.CI ? /@visual/ : undefined,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "test-results/e2e-report" }]],
  // 기준 이미지는 스펙 옆 폴더에, OS 별 접미사로 둬요.
  snapshotPathTemplate: "{testDir}/__screens__/{projectName}/{arg}-{platform}{ext}",
  use: {
    baseURL: `http://127.0.0.1:${E2E.web}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "phone-light", use: { ...phone, colorScheme: "light" } },
    { name: "phone-dark", use: { ...phone, colorScheme: "dark" }, grep: /@layout|@visual/ },
    // 좁은 폰(보급형 360dp)에서도 규칙을 지켜요.
    { name: "phone-narrow", use: { ...phone, viewport: { width: 360, height: 780 }, deviceScaleFactor: 3 }, grep: /@layout|@visual/ },
    // 실기기: scripts/verify.mjs 가 e2e 앱을 설치하고 WebView 디버깅 포트를 넘긴 뒤에만 켜져요.
    // 스크린샷 기준은 헤드리스에서만 비교해요(기기는 상태 표시줄·글꼴이 달라요).
    ...(process.env.JARI_DEVICE_CDP ? [{ name: "device", use: { baseURL: "https://localhost" }, grepInvert: /@visual/ }] : []),
  ],
  webServer: [
    {
      command: `uv run --frozen python tests/e2e/stack.py --api-port ${E2E.api} --control-port ${E2E.control} --redis-port ${E2E.redis} --origin http://127.0.0.1:${E2E.web}`,
      cwd: "backend",
      url: `http://127.0.0.1:${E2E.control}/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      stdout: "pipe",
    },
    {
      command: `npx vite build --mode e2e --outDir dist-e2e --emptyOutDir && npx vite preview --outDir dist-e2e --host 127.0.0.1 --port ${E2E.web} --strictPort`,
      env: { VITE_API_BASE_URL: `http://127.0.0.1:${E2E.api}` },
      url: `http://127.0.0.1:${E2E.web}`,
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
