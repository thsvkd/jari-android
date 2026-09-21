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

export type ApiErrorKind = "auth" | "offline" | "server" | "configuration" | "stale";

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

interface HttpApiOptions {
  baseUrl: string;
  tokenStorage: ReturnType<typeof createSessionStorage>;
  fetcher?: typeof fetch;
  timeZone?: string;
  onAuthExpired?: () => void;
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

  async function request<T>(
    path: string,
    init: { method?: "GET" | "POST" | "DELETE"; body?: unknown; authenticated?: boolean; revokedToken?: string } = {},
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

    let response: Response;
    try {
      response = await fetcher(`${root}${path}`, {
        method: init.method ?? "POST",
        headers,
        body: init.body === undefined ? undefined : JSON.stringify(init.body),
      });
    } catch {
      throw new ApiError("서버에 연결하지 못했어요. 인터넷 연결을 확인해 주세요.", 0, "offline");
    }

    const payload = (await response.json().catch(() => ({}))) as { error?: unknown };
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
    status: () => request<StatusResult>("/status", { method: "GET" }),
  };
}
