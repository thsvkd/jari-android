import { afterEach, describe, expect, it, vi } from "vitest";

import { createHttpApi, createSessionStorage } from "./api";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";
import { normalizeCapabilities } from "./model";

// 에이전트 연결(명세 .omc/plans/mcp-agent-spec.md §2·§7). 토글은 POST 예요: 앱 요청은 GET·POST·DELETE 뿐이고,
// 서버 CORS 도 그 셋만 허용해요(PATCH 는 WebView 의 preflight 에서 막혀요).

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const mounted: JariApp[] = [];

function httpApi() {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ ok: true, agents: [] }));
  const tokenStorage = createSessionStorage({
    write: async () => undefined,
    read: async () => "opaque-session",
    clear: async () => undefined,
  });
  const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage, fetcher, timeZone: "Asia/Seoul" });
  const sent = () => {
    const [url, init] = fetcher.mock.calls.at(-1)!;
    return {
      url,
      method: init?.method,
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
      auth: new Headers(init?.headers).get("Authorization"),
    };
  };
  return { api, sent };
}

describe("에이전트 연결 HTTP API", () => {
  it("연결 목록을 읽어요", async () => {
    const { api, sent } = httpApi();
    await api.agents();
    expect(sent()).toMatchObject({ url: "https://jari.example/api/mobile/agents", method: "GET", body: undefined });
  });

  it("예약 허용을 POST 로 바꾸고 연결 id 는 경로에 맞게 감싸요", async () => {
    const { api, sent } = httpApi();
    await api.agentSetBooking("grant/1", true);
    expect(sent()).toMatchObject({
      url: "https://jari.example/api/mobile/agents/grant%2F1",
      method: "POST",
      body: { allowBooking: true },
    });
  });

  it("연결 끊기는 DELETE 예요", async () => {
    const { api, sent } = httpApi();
    await api.agentDisconnect("grant/1");
    expect(sent()).toMatchObject({ url: "https://jari.example/api/mobile/agents/grant%2F1", method: "DELETE" });
  });
});

describe("에이전트 연결 기능 표시", () => {
  it("서버가 켰을 때만 켜져요", () => {
    expect(normalizeCapabilities({ agents: true } as never).agents).toBe(true);
    expect(normalizeCapabilities({}).agents).toBe(false);
    expect(normalizeCapabilities({ agents: "yes" } as never).agents).toBe(false);
  });
});

describe("체험 모드의 에이전트 연결", () => {
  // 체험 모드는 브라우저 로그인이 없어 처음부터 claude.ai 의 "Claude" 연결 하나(예약 허용 꺼짐)를 보여 줘요.
  it("네트워크 없이 모든 메서드가 있어요", async () => {
    const network = vi.spyOn(globalThis, "fetch");
    const api = createDemoApi(() => new Date("2026-10-07T00:00:00Z"));
    for (const name of ["agents", "agentSetBooking", "agentDisconnect"] as const) {
      expect(typeof api[name]).toBe("function");
    }
    const bootstrap = await api.bootstrap();
    expect(bootstrap.capabilities.agents).toBe(true);
    expect(bootstrap.agents?.mcpUrl).toMatch(/^https:\/\/[^/]+\/mcp$/);
    expect(network).not.toHaveBeenCalled();
  });

  it("처음 목록은 Claude 연결 하나이고, 켜기와 끊기가 목록에 반영돼요", async () => {
    const api = createDemoApi(() => new Date("2026-10-07T00:00:00Z"));
    const [agent] = (await api.agents()).agents;
    expect(agent).toMatchObject({ name: "Claude", host: "claude.ai", local: false, allowBooking: false });
    await api.agentSetBooking(agent!.id, true);
    expect((await api.agents()).agents[0]?.allowBooking).toBe(true);
    await api.agentDisconnect(agent!.id);
    expect((await api.agents()).agents).toHaveLength(0);
  });
});

describe("연결 화면의 주소", () => {
  afterEach(() => {
    for (const app of mounted.splice(0)) app.dispose();
    document.body.replaceChildren();
  });

  // §11 M6: 앱은 baseUrl 로 주소를 만들지 않고 서버가 bootstrap 으로 준 mcpUrl 을 보여 줘요.
  it("서버가 준 MCP 주소를 그대로 보여요", async () => {
    const demo = createDemoApi();
    const mcpUrl = "https://elsewhere.example/mcp";
    const api = {
      ...demo,
      bootstrap: async () => {
        const state = await demo.bootstrap();
        return { ...state, capabilities: { ...state.capabilities, agents: true }, agents: { mcpUrl } };
      },
    };
    const root = document.createElement("div");
    document.body.append(root);
    const app = new JariApp(root, api, { demoMode: false });
    mounted.push(app);
    await app.start(true);
    app.navigate("settings");
    expect(root.textContent).toContain("에이전트 연결");
    app.navigate("agents" as never);
    await vi.waitFor(() => expect(root.textContent).toContain(mcpUrl));
  });

  it("코드 입력 없이 브라우저 로그인으로 연결한다고 안내해요", async () => {
    const root = document.createElement("div");
    document.body.append(root);
    const app = new JariApp(root, createDemoApi(), { demoMode: false });
    mounted.push(app);
    await app.start(true);
    app.navigate("agents" as never);
    await vi.waitFor(() => expect(root.textContent).toContain("브라우저에서 자리났다 계정으로 로그인해 허용해요"));
    expect(root.textContent).toContain("새 연결은 조회만 할 수 있고, 예약은 아래 목록에서 켜요.");
    expect(root.querySelector("form")).toBeNull();
    expect(root.textContent).not.toContain("코드");
  });

  it("앱으로 돌아오면 연결 목록을 다시 읽어요", async () => {
    // 연결은 브라우저에서 끝나요. 연결 화면을 연 채로 다녀오면 새 연결이 보여야 해요.
    const api = createDemoApi();
    const agents = vi.spyOn(api, "agents");
    const root = document.createElement("div");
    document.body.append(root);
    const app = new JariApp(root, api, { demoMode: false });
    mounted.push(app);
    await app.start(true);
    app.navigate("agents" as never);
    await vi.waitFor(() => expect(agents).toHaveBeenCalledTimes(1));
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() => expect(agents).toHaveBeenCalledTimes(2));
    app.navigate("settings");
    document.dispatchEvent(new Event("visibilitychange"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(agents).toHaveBeenCalledTimes(2);
  });

  it("서버가 기능을 끄면 설정에 행이 없어요", async () => {
    const demo = createDemoApi();
    const api = {
      ...demo,
      bootstrap: async () => {
        const state = await demo.bootstrap();
        const { agents: _agents, ...rest } = state as typeof state & { agents?: unknown };
        return { ...rest, capabilities: { ...state.capabilities, agents: false } };
      },
    };
    const root = document.createElement("div");
    document.body.append(root);
    const app = new JariApp(root, api, { demoMode: false });
    mounted.push(app);
    await app.start(true);
    app.navigate("settings");
    expect(root.textContent).not.toContain("에이전트 연결");
  });
});
