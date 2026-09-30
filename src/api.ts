import type {
  AuthResult,
  BookingPayload,
  BootstrapState,
  Conditions,
  Favourite,
  InviteResult,
  MobileApi,
  NotificationItem,
  PendingReservation,
  SeatCarOption,
  SeatClass,
  SeatInventoriesResult,
  SeatInventory,
  SeatMapSeat,
  SearchResult,
  StatusResult,
  TrainsResult,
} from "./types";

// "timeout": no answer in time. The request may well have reached the server, so it is not "offline".
export type ApiErrorKind = "auth" | "offline" | "timeout" | "server" | "configuration" | "stale";

export class ApiError extends Error {
  readonly status: number;
  readonly kind: ApiErrorKind;

  constructor(message: string, status: number, kind: ApiErrorKind = "server") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind;
  }
}

interface TokenStorage {
  read(): Promise<string | null>;
  clear(): Promise<void>;
}

// All production token reads and writes share this queue, including native storage.
export function createSessionStorage(storage: TokenStorage & { write(token: string): Promise<void> }) {
  let generation = 0;
  let queue: Promise<unknown> = Promise.resolve();
  function serial<T>(work: () => Promise<T>): Promise<T> {
    const result = queue.then(work);
    queue = result.catch(() => undefined);
    return result;
  }
  return {
    generation: () => generation,
    read: () => serial(() => storage.read()),
    write: (token: string) => {
      generation += 1;
      return serial(() => storage.write(token));
    },
    clear: () => {
      generation += 1;
      return serial(() => storage.clear());
    },
    expire: (token: string, expected: number, onExpired?: () => void) => serial(async () => {
      if (generation !== expected || await storage.read() !== token || generation !== expected) return false;
      await storage.clear();
      // A login queued during native clear owns the next session and suppresses this callback.
      if (generation !== expected) return false;
      generation += 1;
      onExpired?.();
      return true;
    }),
  };
}

/** A request that got no HTTP answer at all. The server never sees these, so the app keeps them and uploads them later. */
export interface ConnectionMiss {
  at: string;
  method: string;
  path: string;
  reason: "network" | "timeout";
  online: boolean;
  visible: boolean;
  elapsedMs: number;
}

// The server takes at most this many per upload.
const MAX_CONNECTION_MISSES = 50;
const CONNECTION_MISSES_KEY = "jari.connectionMisses";

type MissStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/** Keeps the latest misses; in localStorage when given one, so misses from before the app was killed still get uploaded. */
export function createConnectionMissLog(storage: MissStorage | null = null) {
  let memory: ConnectionMiss[] = [];
  const read = (): ConnectionMiss[] => {
    if (!storage) return memory;
    try {
      const parsed: unknown = JSON.parse(storage.getItem(CONNECTION_MISSES_KEY) ?? "[]");
      return Array.isArray(parsed) ? (parsed as ConnectionMiss[]) : [];
    } catch {
      return memory;
    }
  };
  const write = (misses: ConnectionMiss[]) => {
    memory = misses;
    try {
      if (misses.length) storage?.setItem(CONNECTION_MISSES_KEY, JSON.stringify(misses));
      else storage?.removeItem(CONNECTION_MISSES_KEY);
    } catch {
      // Diagnostics only; storage that throws keeps them in memory.
    }
  };
  return {
    read,
    add: (miss: ConnectionMiss) => write([...read(), miss].slice(-MAX_CONNECTION_MISSES)),
    /** Removes what was uploaded, keeping misses recorded while the upload was out. */
    drop: (sent: ConnectionMiss[]) => {
      const uploaded = new Set(sent.map((miss) => JSON.stringify(miss)));
      write(read().filter((miss) => !uploaded.has(JSON.stringify(miss))));
    },
  };
}

// Only the status poll has a deadline. It reads Redis, but waits behind the user's lock while a seat map or reservation
// talks to Korail. Requests that go to Korail get none: giving up on one can hide a reservation that went through.
const POLL_TIMEOUT_MS = 20_000;

interface HttpApiOptions {
  baseUrl: string;
  tokenStorage: ReturnType<typeof createSessionStorage>;
  fetcher?: typeof fetch;
  timeZone?: string;
  onAuthExpired?: () => void;
  connectionMisses?: ReturnType<typeof createConnectionMissLog>;
}

// Opaque train keys and favourite ids say nothing about the failure; the route does.
function routeOf(path: string): string {
  return path
    .split("?")[0]!
    .replace(/^\/trains\/[^/]+/, "/trains/:key")
    .replace(/^\/favourites\/[^/]+/, "/favourites/:id");
}

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

export function resolveApiBase(configured: string, pageOrigin: string, native = false): string {
  const candidate = configured.trim();
  if (!candidate) {
    if (native) {
      throw new ApiError("Android 앱에서 실서버를 사용하려면 HTTPS API 주소가 필요해요.", 0, "configuration");
    }
    const page = new URL(pageOrigin);
    if (page.protocol !== "https:" && !LOCAL_HOSTS.has(page.hostname)) {
      throw new ApiError("실서버 API에는 HTTPS 주소가 필요해요.", 0, "configuration");
    }
    return "";
  }
  const url = new URL(candidate);
  if (url.protocol !== "https:" && !(url.protocol === "http:" && LOCAL_HOSTS.has(url.hostname))) {
    throw new ApiError("실서버 API에는 HTTPS 주소가 필요해요.", 0, "configuration");
  }
  return url.href.replace(/\/$/, "");
}

export function createHttpApi(options: HttpApiOptions): MobileApi {
  const fetcher = options.fetcher ?? fetch;
  const root = `${options.baseUrl.replace(/\/$/, "")}/api/mobile`;
  const timeZone = options.timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? "UTC";
  const misses = options.connectionMisses ?? createConnectionMissLog();
  let uploading = false;
  // After a 429 the upload waits a minute: every retry would count against the same per-user API limit as real requests.
  let uploadAfter = 0;

  async function uploadMisses(): Promise<void> {
    const pending = misses.read();
    if (uploading || !pending.length || Date.now() < uploadAfter) return;
    uploading = true;
    try {
      await request<{ ok: boolean }>("/diagnostics", { body: { misses: pending } });
      misses.drop(pending);
    } catch (error) {
      // A 400 will never be accepted; anything else is tried again after the next success.
      if (error instanceof ApiError && error.status === 400) misses.drop(pending);
      if (error instanceof ApiError && error.status === 429) uploadAfter = Date.now() + 60_000;
    } finally {
      uploading = false;
    }
  }

  async function request<T>(
    path: string,
    init: {
      method?: "GET" | "POST" | "DELETE";
      body?: unknown;
      authenticated?: boolean;
      revokedToken?: string;
      timeoutMs?: number;
    } = {},
  ): Promise<T> {
    const authenticated = init.authenticated !== false;
    const generation = options.tokenStorage.generation();
    const token = authenticated ? await options.tokenStorage.read() : init.revokedToken ?? null;
    if (authenticated && generation !== options.tokenStorage.generation()) {
      throw new ApiError("이전 로그인에서 시작한 요청이라 중단했어요.", 0, "stale");
    }
    if (authenticated && !token) {
      throw new ApiError("로그인이 필요해요.", 401, "auth");
    }
    const headers = new Headers({ Accept: "application/json", "X-User-Timezone": timeZone });
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (init.body !== undefined) headers.set("Content-Type", "application/json");

    // Without a deadline a poll the network swallowed stays pending, and nothing ever reports it.
    const controller = new AbortController();
    let timedOut = false;
    const timer = init.timeoutMs === undefined ? undefined : setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, init.timeoutMs);
    const started = Date.now();
    // Whether the request left from the foreground; one sent from the background proves little about the server.
    const visible = document.visibilityState !== "hidden";
    let response: Response;
    let text: string;
    try {
      response = await fetcher(`${root}${path}`, {
        method: init.method ?? "POST",
        headers,
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
        signal: controller.signal,
      });
      // The body is inside the deadline too: headers and body arrive separately, and a body that never comes would
      // otherwise leave the poll pending.
      text = await response.text();
    } catch {
      // A Cloudflare error page has no CORS headers either, so it lands here too, not as an HTTP status.
      if (path !== "/diagnostics") {
        misses.add({
          at: new Date(started).toISOString(),
          method: init.method ?? "POST",
          path: routeOf(path),
          reason: timedOut ? "timeout" : "network",
          online: navigator.onLine,
          visible,
          elapsedMs: Date.now() - started,
        });
      }
      if (timedOut) throw new ApiError("서버가 제때 답하지 않았어요. 잠시 후 다시 확인해요.", 0, "timeout");
      throw new ApiError("서버에 연결하지 못했어요. 인터넷 연결을 확인해 주세요.", 0, "offline");
    } finally {
      clearTimeout(timer);
    }
    // Only once the session has proven good: an upload answered 401 would expire it a second time.
    if (response.ok && authenticated && path !== "/diagnostics") void uploadMisses();

    let payload: { error?: unknown } = {};
    try {
      payload = (JSON.parse(text) ?? {}) as { error?: unknown };
    } catch {
      // Not JSON (an empty body, a proxy page); the status still says what happened.
    }
    if (!response.ok) {
      const message =
        typeof payload.error === "string" ? payload.error : "요청을 처리하지 못했어요.";
      if (response.status === 401 && authenticated) {
        const expired = await options.tokenStorage.expire(token!, generation, options.onAuthExpired);
        throw new ApiError(message, response.status, expired ? "auth" : "stale");
      }
      throw new ApiError(message, response.status, "server");
    }
    return payload as T;
  }

  return {
    registerApp: (input) => request<AuthResult>("/auth/register", { body: input, authenticated: false }),
    login: (input) => request<AuthResult>("/auth/login", { body: input, authenticated: false }),
    createInvite: (input = {}) => request<InviteResult>("/invites", { body: input }),
    logoutApp: async () => {
      const generation = options.tokenStorage.generation();
      const token = await options.tokenStorage.read();
      if (generation !== options.tokenStorage.generation()) return { ok: true };
      // Clear locally before waiting on the network, then revoke only the captured token.
      await options.tokenStorage.clear();
      if (!token) return { ok: true };
      return request<{ ok: boolean }>("/auth/logout", {
        body: {}, authenticated: false, revokedToken: token,
      });
    },
    bootstrap: () => request<BootstrapState>("/bootstrap"),
    railwayRegister: (input) => request<{ registered: boolean }>("/register", { body: input }),
    railwayLogout: () => request<{ registered: boolean }>("/logout", { body: {} }),
    trains: (payload: { conditions: Conditions }) => request<TrainsResult>("/trains", { body: payload }),
    seatCars: (trainKey: string, seatClass: SeatClass, passengerCount: number) =>
      request<{ cars: SeatCarOption[] }>(
        `/trains/${encodeURIComponent(trainKey)}/cars?seatClass=${encodeURIComponent(seatClass)}&passengerCount=${passengerCount}`,
        { method: "GET" },
      ),
    seatInventory: (trainKey: string, carNo: number, seatClass: SeatClass, passengerCount: number) =>
      request<SeatInventory>(
        `/trains/${encodeURIComponent(trainKey)}/cars/${carNo}/seats?seatClass=${encodeURIComponent(seatClass)}&passengerCount=${passengerCount}`,
        { method: "GET" },
      ),
    seatInventories: (trainKey: string, seatClass: SeatClass, passengerCount: number) =>
      request<SeatInventoriesResult>(
        `/trains/${encodeURIComponent(trainKey)}/seats?seatClass=${encodeURIComponent(seatClass)}&passengerCount=${passengerCount}`,
        { method: "GET" },
      ),
    reserveDesignated: (payload: { trainKey: string; seatClass: SeatClass; passengerCount: number; carNo: number; seats: SeatMapSeat[] }) =>
      request("/reservations/designated", { body: payload }),
    search: (payload: BookingPayload) => request<SearchResult>("/search", { body: payload }),
    schedule: (payload) =>
      request<{ scheduled: boolean; startAt: string }>("/schedule", { body: payload }),
    cancelSearch: () => request<{ stopped: boolean; unscheduled: boolean }>("/search/cancel", { body: {} }),
    cancelReservations: () =>
      request<{ cancelled: boolean; pending: PendingReservation[] }>("/reservations/cancel", { body: {} }),
    requestAccess: () => request<{ requested: boolean; approved?: boolean }>("/access-request", { body: {} }),
    favourites: () => request<{ favourites: Favourite[] } | Favourite[]>("/favourites", { method: "GET" }),
    saveFavourite: (payload) =>
      request<{ saved: boolean; favourites: Favourite[] }>("/favourites", { body: payload }),
    deleteFavourite: (id) =>
      request<{ deleted: boolean; favourites: Favourite[] }>(`/favourites/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
    setNotify: (minutes) =>
      request<{ notifyMinutes: number }>("/notify", { body: { minutes } }),
    notifications: () =>
      request<{ items: NotificationItem[]; pushAvailable: boolean }>("/notifications", { method: "GET" }),
    registerDevice: (token) =>
      request<{ ok?: boolean; pushAvailable?: boolean }>("/devices", {
        body: { token, platform: "android" },
      }),
    deleteDevice: (token) =>
      request<{ ok?: boolean; pushAvailable?: boolean }>("/devices", {
        method: "DELETE",
        body: { token },
      }),
    status: () => request<StatusResult>("/status", { method: "GET", timeoutMs: POLL_TIMEOUT_MS }),
  };
}
