import { beforeEach, describe, expect, it, vi } from "vitest";

import { createHttpApi, createSessionStorage, resolveApiBase } from "./api";

const response = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("mobile API authentication boundary", () => {
  let token: string | null;
  let cleared: number;

  beforeEach(() => {
    token = "opaque-session";
    cleared = 0;
  });

  const tokenStorage = createSessionStorage({
    write: async (next) => { token = next; },
    read: async () => token,
    clear: async () => {
      token = null;
      cleared += 1;
    },
  });

  it("sends the opaque session and timezone without a client identity", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({ running: null }));
    const api = createHttpApi({
      baseUrl: "https://jari.example",
      tokenStorage,
      fetcher,
      timeZone: "Asia/Seoul",
    });

    await api.bootstrap();

    expect(fetcher).toHaveBeenCalledOnce();
    const [url, init] = fetcher.mock.calls[0]!;
    expect(url).toBe("https://jari.example/api/mobile/bootstrap");
    expect(init?.method).toBe("POST");
    expect(new Headers(init?.headers).get("Authorization")).toBe("Bearer opaque-session");
    expect(new Headers(init?.headers).get("X-User-Timezone")).toBe("Asia/Seoul");
    expect(init?.body).toBeUndefined();
  });

  it("clears a rejected session and reports authentication expiry", async () => {
    const expired = vi.fn();
    const api = createHttpApi({
      baseUrl: "https://jari.example",
      tokenStorage,
      fetcher: vi.fn<typeof fetch>().mockResolvedValue(response({ error: "세션 만료" }, 401)),
      onAuthExpired: expired,
    });

    await expect(api.bootstrap()).rejects.toMatchObject({
      name: "ApiError",
      kind: "auth",
      status: 401,
    });
    expect(cleared).toBe(1);
    expect(expired).toHaveBeenCalledOnce();
  });

  it("distinguishes an offline request from an empty railway result", async () => {
    const api = createHttpApi({
      baseUrl: "https://jari.example",
      tokenStorage,
      fetcher: vi.fn<typeof fetch>().mockRejectedValue(new TypeError("network")),
    });

    await expect(api.status()).rejects.toEqual(
      expect.objectContaining({ kind: "offline", status: 0 }),
    );
  });

  it("does not send a stale session to login or invitation signup", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      response({ token: "new", user: { id: "u1", username: "mint" }, expiresAt: "future" }),
    );
    const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage, fetcher });

    await api.login({ username: "mint", password: "long password" });

    const [, init] = fetcher.mock.calls[0]!;
    expect(new Headers(init?.headers).has("Authorization")).toBe(false);
    expect(JSON.parse(String(init?.body))).toEqual({
      username: "mint",
      password: "long password",
    });
  });
});

describe("API base safety", () => {
  it("accepts HTTPS and local browser debugging", () => {
    expect(resolveApiBase("https://jari.example", "https://app.example")).toBe(
      "https://jari.example",
    );
    expect(resolveApiBase("http://127.0.0.1:5000", "http://127.0.0.1:4173")).toBe(
      "http://127.0.0.1:5000",
    );
  });

  it("rejects cleartext remote API hosts", () => {
    expect(() => resolveApiBase("http://192.168.0.50:5000", "http://localhost:4173")).toThrow(
      /HTTPS/,
    );
  });

  it("requires an explicit API host inside a native WebView", () => {
    expect(() => resolveApiBase("", "https://localhost", true)).toThrow(/API 주소/);
  });
});

it("a deferred A 401 cannot erase the newer B session or expire B", async () => {
  let token: string | null = "A";
  let finish!: (value: Response) => void;
  const pending = new Promise<Response>((resolve) => { finish = resolve; });
  const expired = vi.fn();
  const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending);
  const api = createHttpApi({ baseUrl: "https://jari.example", fetcher, onAuthExpired: expired, tokenStorage: createSessionStorage({
    write: async (next) => { token = next; },
    read: async () => token,
    clear: async () => { token = null; },
  }) });
  const request = api.status().catch((error) => error);
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
  token = "B";
  finish(response({ error: "expired A" }, 401));
  await request;
  expect(token).toBe("B");
  expect(expired).not.toHaveBeenCalled();
});

it("serializes deferred token clearing with a new login and suppresses stale expiry callbacks", async () => {
  const { createSessionStorage } = await import("./api");
  let token: string | null = "A";
  let finishClear!: () => void;
  const clearing = new Promise<void>((resolve) => { finishClear = resolve; });
  const clearStarted = vi.fn();
  const storage = createSessionStorage({
    read: async () => token,
    write: async (next) => { token = next; },
    clear: async () => { clearStarted(); await clearing; token = null; },
  });
  const expired = vi.fn();
  const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage: storage,
    fetcher: vi.fn<typeof fetch>().mockResolvedValue(response({}, 401)), onAuthExpired: expired });
  const request = api.status().catch((error) => error);
  await vi.waitFor(() => expect(clearStarted).toHaveBeenCalledOnce());
  const login = storage.write("B");
  finishClear();
  await Promise.all([request, login]);
  expect(await storage.read()).toBe("B");
  expect(expired).not.toHaveBeenCalled();
});

it("an invalidated old 401 is reported as stale rather than expiring the current app", async () => {
  let token: string | null = "A";
  const storage = createSessionStorage({ read: async () => token, write: async (next) => { token = next; }, clear: async () => { token = null; } });
  let finish!: (value: Response) => void;
  const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage: storage, fetcher });
  const request = api.status().catch((error) => error);
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
  await storage.write("B");
  finish(response({}, 401));
  expect(await request).toMatchObject({ kind: "stale" });
});

it("does not clear B when B is queued during a deferred expiry storage read", async () => {
  let token: string | null = "A";
  let finishRead!: (value: string | null) => void;
  let reads = 0;
  const storage = createSessionStorage({
    read: () => ++reads === 2 ? new Promise((resolve) => { finishRead = resolve; }) : Promise.resolve(token),
    write: async (next) => { token = next; },
    clear: async () => { token = null; },
  });
  const expired = vi.fn();
  const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage: storage, onAuthExpired: expired,
    fetcher: vi.fn<typeof fetch>().mockResolvedValue(response({}, 401)) });
  const request = api.status().catch((error) => error);
  await vi.waitFor(() => expect(reads).toBe(2));
  const login = storage.write("B");
  finishRead("A");
  await Promise.all([request, login]);
  expect(await storage.read()).toBe("B");
  expect(expired).not.toHaveBeenCalled();
});

it("expires concurrent rejected requests only once", async () => {
  let token: string | null = "A";
  const storage = createSessionStorage({ read: async () => token, write: async (next) => { token = next; }, clear: async () => { token = null; } });
  const expired = vi.fn();
  const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage: storage, onAuthExpired: expired,
    fetcher: vi.fn<typeof fetch>().mockResolvedValue(response({}, 401)) });
  await Promise.allSettled([api.status(), api.bootstrap()]);
  expect(await storage.read()).toBeNull();
  expect(expired).toHaveBeenCalledOnce();
});

it("does not send an old operation with B credentials when token reading overlaps login", async () => {
  let finishRead!: (value: string | null) => void;
  let token: string | null = "A";
  const storage = createSessionStorage({ read: () => new Promise((resolve) => { finishRead = resolve; }), write: async (next) => { token = next; }, clear: async () => { token = null; } });
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response({}));
  const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage: storage, fetcher });
  const request = api.cancelSearch().catch((error) => error);
  await vi.waitFor(() => expect(finishRead).toBeDefined());
  const login = storage.write("B");
  finishRead("B");
  await login;
  expect(await request).toMatchObject({ kind: "stale" });
  expect(fetcher).not.toHaveBeenCalled();
  expect(token).toBe("B");
});

it("logout clears local A before waiting for revocation and never revokes B", async () => {
  let token: string | null = "A";
  const storage = createSessionStorage({ read: async () => token, write: async (next) => { token = next; }, clear: async () => { token = null; } });
  let finish!: (value: Response) => void;
  const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  const expired = vi.fn();
  const api = createHttpApi({ baseUrl: "https://jari.example", tokenStorage: storage, fetcher, onAuthExpired: expired });
  const logout = api.logoutApp().catch((error) => error);
  await vi.waitFor(() => expect(fetcher).toHaveBeenCalledOnce());
  expect(await storage.read()).toBeNull();
  await storage.write("B");
  finish(response({}, 401));
  await logout;
  expect(new Headers(fetcher.mock.calls[0]![1]?.headers).get("Authorization")).toBe("Bearer A");
  expect(await storage.read()).toBe("B");
  expect(expired).not.toHaveBeenCalled();
});
