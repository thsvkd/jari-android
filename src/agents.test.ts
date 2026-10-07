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
  it("코드로 대기 중인 요청을 찾아요", async () => {
    const { api, sent } = httpApi();
    await api.agentLookup("abcd-efgh");
    const request = sent();
    expect(request.url).toBe("https://jari.example/api/mobile/agents/requests/lookup");
    expect(request.method).toBe("POST");
    expect(request.auth).toBe("Bearer opaque-session");
    expect(String(request.body.code).replace(/[-\s]/g, "").toUpperCase()).toBe("ABCDEFGH");
  });

  // §12 S-H1: 승인은 예약 허용을 보내지 않아요. 새 연결은 늘 읽기 전용이고, 예약 허용은 목록에서 켜요.
  it("승인은 요청 id 만 보내요", async () => {
    const { api, sent } = httpApi();
    await api.agentApprove("req-1");
    expect(sent()).toMatchObject({
      url: "https://jari.example/api/mobile/agents/requests/approve",
      method: "POST",
    });
    expect(sent().body).toEqual({ requestId: "req-1" });
  });

  it("예약 허용을 넘겨도 승인 본문에 싣지 않아요", async () => {
    const { api, sent } = httpApi();
    await (api.agentApprove as (...args: unknown[]) => Promise<unknown>)("req-1", true);
    expect(sent().body).toEqual({ requestId: "req-1" });
  });

  it("거절은 요청 하나만 보내요", async () => {
    const { api, sent } = httpApi();
    await api.agentDeny("req-1");
    expect(sent()).toMatchObject({
      url: "https://jari.example/api/mobile/agents/requests/deny",
      method: "POST",
      body: { requestId: "req-1" },
    });
  });

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

  it("틀린 코드는 서버 문구 그대로 오류가 돼요", async () => {
    const tokenStorage = createSessionStorage({
      write: async () => undefined,
      read: async () => "opaque-session",
      clear: async () => undefined,
    });
    const api = createHttpApi({
      baseUrl: "https://jari.example",
      tokenStorage,
      fetcher: vi.fn<typeof fetch>().mockResolvedValue(response({ error: "코드를 다시 확인해 주세요." }, 404)),
    });
    await expect(api.agentLookup("ZZZZ-ZZZZ")).rejects.toMatchObject({ status: 404, message: "코드를 다시 확인해 주세요." });
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
  // §11 L9: 데모는 WXYZ-2345 만 받고, 처음에는 claude.ai 의 "Claude" 연결 하나(예약 허용 꺼짐)가 있어요.
  it("네트워크 없이 모든 메서드가 있어요", async () => {
    const network = vi.spyOn(globalThis, "fetch");
    const api = createDemoApi(() => new Date("2026-10-07T00:00:00Z"));
    for (const name of ["agentLookup", "agentApprove", "agentDeny", "agents", "agentSetBooking", "agentDisconnect"] as const) {
      expect(typeof api[name]).toBe("function");
    }
    const bootstrap = await api.bootstrap();
    expect(bootstrap.capabilities.agents).toBe(true);
    expect(bootstrap.agents?.mcpUrl).toMatch(/^https:\/\/.+\/api\/mobile\/mcp$/);
    expect(network).not.toHaveBeenCalled();
  });

  it("처음 목록은 Claude 연결 하나예요", async () => {
    const api = createDemoApi(() => new Date("2026-10-07T00:00:00Z"));
    const { agents } = await api.agents();
    expect(agents).toHaveLength(1);
    expect(agents[0]).toMatchObject({ name: "Claude", host: "claude.ai", local: false, allowBooking: false });
  });

  it.each(["WXYZ-2345", "wxyz2345", " wxyz-2345 "])("데모 코드 %j 는 대소문자·하이픈·공백과 상관없이 받아요", async (code) => {
    const api = createDemoApi(() => new Date("2026-10-07T00:00:00Z"));
    const found = await api.agentLookup(code);
    expect(found.requestId).toBeTruthy();
    expect(found.client.name).toBeTruthy();
  });

  it("다른 코드는 거절해요", async () => {
    const api = createDemoApi(() => new Date("2026-10-07T00:00:00Z"));
    await expect(api.agentLookup("ZZZZ-ZZZZ")).rejects.toThrow("코드를 다시 확인해 주세요.");
  });

  it("데모 코드로 승인하면 예약 허용이 꺼진 연결이 하나 늘고, 켜기와 끊기가 목록에 반영돼요", async () => {
    const api = createDemoApi(() => new Date("2026-10-07T00:00:00Z"));
    const { requestId } = await api.agentLookup("WXYZ-2345");
    const before = new Set((await api.agents()).agents.map((agent) => agent.id));
    await api.agentApprove(requestId);
    const after = (await api.agents()).agents;
    expect(after).toHaveLength(2);
    const added = after.find((agent) => !before.has(agent.id))!;
    // §12 S-H1: 새 연결은 예약 허용이 꺼진 채 시작하고, 목록에서 켜요.
    expect(added.allowBooking).toBe(false);
    await api.agentSetBooking(added.id, true);
    expect((await api.agents()).agents.find((agent) => agent.id === added.id)?.allowBooking).toBe(true);
    await api.agentDisconnect(added.id);
    expect((await api.agents()).agents).toHaveLength(1);
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
    const mcpUrl = "https://elsewhere.example/api/mobile/mcp";
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
