import "./styles.css";

import { Capacitor } from "@capacitor/core";

import { ApiError, createHttpApi, createSessionStorage, resolveApiBase } from "./api";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";
import {
  applyStatusBarStyle,
  clearToken,
  followKeyboard,
  initializePlatform,
  readToken,
  writeToken,
} from "./platform";

document.documentElement.classList.toggle("native-platform", Capacitor.isNativePlatform());

const root = document.querySelector<HTMLElement>("#app");
if (!root) throw new Error("앱을 표시할 영역을 찾을 수 없어요.");
const rootElement = root;
followKeyboard(rootElement);

const query = new URLSearchParams(window.location.search);
const demoMode = query.get("demo") === "1" || import.meta.env.VITE_DEMO_MODE === "true";
let app: JariApp | undefined;

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
          app?.notify("이 휴대폰에서 알림을 받을 수 있어요.");
        },
        onPushError: (message) => app?.notify(message),
        enablePushRegistration,
      });
    };

    app = new JariApp(rootElement, api, {
      demoMode,
      onToken: session.write,
      onBootstrap: async () => {
        await initialize(false);
      },
      onRequestPush: async () => {
        await initialize(true);
      },
      onTheme: applyStatusBarStyle,
    });

    const authenticated = demoMode || Boolean(await session.read());
    await app.start(authenticated);
  } catch (error) {
    const message =
      error instanceof ApiError || error instanceof Error
        ? error.message
        : "앱을 시작하지 못했어요.";
    const page = document.createElement("main");
    page.className = "fatal";
    const mark = document.createElement("span");
    mark.textContent = "!";
    const title = document.createElement("h1");
    title.textContent = "앱을 열지 못했어요";
    const detail = document.createElement("p");
    detail.textContent = message;
    const hint = document.createElement("p");
    hint.className = "muted";
    hint.textContent = "실서버를 사용하려면 HTTPS API 주소가 필요해요. 화면만 살펴보려면 주소 끝에 ?demo=1을 붙여 주세요.";
    page.append(mark, title, detail, hint);
    rootElement.replaceChildren(page);
  }
}

void launch();
