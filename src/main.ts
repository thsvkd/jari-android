import "./styles.css";

import { Capacitor } from "@capacitor/core";

import { ApiError, createHttpApi, createSessionStorage, resolveApiBase } from "./api";
import { TeumApp } from "./app";
import { createDemoApi } from "./demo";
import {
  clearToken,
  disposePlatform,
  initializePlatform,
  readToken,
  writeToken,
} from "./platform";

document.documentElement.classList.toggle("native-platform", Capacitor.isNativePlatform());

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("앱을 표시할 영역을 찾을 수 없습니다.");
const rootElement = root;

const query = new URLSearchParams(window.location.search);
const demoMode = query.get("demo") === "1" || import.meta.env.VITE_DEMO_MODE === "true";
let app: TeumApp | undefined;

async function launch(): Promise<void> {
  try {
    const apiBase = demoMode
      ? ""
      : resolveApiBase(
          import.meta.env.VITE_API_BASE_URL ?? "",
          window.location.origin,
          Capacitor.isNativePlatform(),
        );
    const session = createSessionStorage({ read: readToken, write: writeToken, clear: clearToken });
    const api = demoMode
      ? createDemoApi()
      : createHttpApi({
          baseUrl: apiBase,
          tokenStorage: session,
          onAuthExpired: () => app?.start(false),
        });

    const initialize = async (enablePushRegistration: boolean): Promise<void> => {
      await initializePlatform({
        onBack: () => app?.back() ?? false,
        onPushToken: async (token) => {
          await api.registerDevice(token);
          app?.notify("이 기기의 알림 토큰을 등록했어요.");
        },
        onPushError: (message) => app?.notify(message),
        enablePushRegistration,
      });
    };

    app = new TeumApp(rootElement, api, {
      demoMode,
      onToken: session.write,
      onBootstrap: async () => {
        await initialize(false);
      },
      onRequestPush: async () => {
        await initialize(true);
      },
    });

    const authenticated = demoMode || Boolean(await session.read());
    await app.start(authenticated);
  } catch (error) {
    const message =
      error instanceof ApiError || error instanceof Error
        ? error.message
        : "앱을 시작하지 못했습니다.";
    const page = document.createElement("main");
    page.className = "fatal";
    const mark = document.createElement("span");
    mark.textContent = "!";
    const title = document.createElement("h1");
    title.textContent = "틈을 열지 못했어요";
    const detail = document.createElement("p");
    detail.textContent = message;
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.textContent = "라이브 앱에는 HTTPS API 주소가 필요합니다. 로컬 화면 검토는 ?demo=1로 열 수 있어요.";
    page.append(mark, title, detail, hint);
    rootElement.replaceChildren(page);
  }
}

window.addEventListener("beforeunload", () => {
  app?.dispose();
  void disposePlatform();
});

void launch();
