import { ApiError } from "./api";
import appPackage from "../package.json";
import {
  buildBookingPayload,
  buildConditions,
  clockFromCompact,
  conditionsToDraft,
  deriveRadarView,
  normalizeCapabilities,
  type BookingDraft,
  type ConnectionState,
} from "./model";
import {
  consecutiveGroups,
  emptySeatFilter,
  filterSeats,
  groupSeatsByLayout,
  maxTrimRows,
  seatColumnSets,
  type SeatFilter,
} from "./seat-map";
import type {
  BootstrapState,
  CancellationWaitPlan,
  Conditions,
  DesignatedReservationResult,
  Favourite,
  MobileApi,
  NotificationItem,
  SearchDescription,
  SeatCarOption,
  SeatClass,
  SeatInventoriesResult,
  SeatInventory,
  SeatMapSeat,
  SeatSelectionMode,
  SeatTarget,
  TrainOption,
} from "./types";

export type AppView =
  | "auth"
  | "home"
  | "journey"
  | "trains"
  | "confirm"
  | "activity"
  | "favourites"
  | "notifications"
  | "settings"
  | "rail-account";

interface AppOptions {
  demoMode: boolean;
  onToken?: (token: string) => Promise<void>;
  onLogout?: () => Promise<void>;
  onBootstrap?: (state: BootstrapState) => Promise<void> | void;
  onRequestPush?: () => Promise<void>;
  onTheme?: (theme: "light" | "dark") => Promise<void> | void;
}

interface SeatDialogState {
  train: TrainOption;
  seatClass: SeatClass;
  mode: SeatSelectionMode;
  cars: SeatCarOption[];
  inventory: SeatInventory | null;
  inventories: Map<number, SeatInventory>;
  carNo: number | null;
  selected: SeatMapSeat[];
  filter: SeatFilter;
  layoutReference: boolean;
  loading: boolean;
  error: string;
  bulkApplying: boolean;
  trimCars: number;
  // Set by "모든 호차에 적용": from then on a condition change re-picks every kept car from the cached inventories,
  // instead of only the car on screen and leaving the others - and the count on the confirm button - as they were.
  appliedToAll: boolean;
}

// Leave at least one car in the middle: a 3-car train allows 1, a 2-car train 0.
const maxTrimCars = (cars: number): number => Math.max(0, Math.floor((cars - 1) / 2));
// The cars "호차 앞뒤 제외" keeps: the formation minus that many cars at each end.
const keptCarNos = (dialog: SeatDialogState): Set<number> => {
  const ordered = [...dialog.cars].sort((a, b) => a.carNo - b.carNo);
  return new Set(ordered.slice(dialog.trimCars, ordered.length - dialog.trimCars).map((car) => car.carNo));
};

const SEAT_OPTIONS: Record<string, string> = {
  "1": "일반실 우선",
  "2": "일반실만",
  "3": "특실 우선",
  "4": "특실만",
};

const NOTIFY_STEPS = [0, 1, 3, 5, 10, 15, 30, 60, 120, 180];

function escapeHtml(value: unknown): string {
  return String(value ?? "").replace(/[&<>"']/g, (character) => {
    const entities: Record<string, string> = {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    };
    return entities[character] ?? character;
  });
}

// True when a seat's label is the real seat number rather than the synthetic column letter the grid uses to lay it out (e.g. 무궁화 "23" vs KTX "3A").
function isNumericSeatLabel(seat: SeatMapSeat): boolean {
  return Boolean(seat.column) && !seat.label.endsWith(seat.column);
}

function formatStamp(value: string | null | undefined): string {
  if (!value) return "시각 정보 없음";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "시각 정보 없음";
  return new Intl.DateTimeFormat("ko-KR", {
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function formatDate(value: string): string {
  if (value.length !== 8) return value || "날짜 미정";
  return `${Number(value.slice(4, 6))}월 ${Number(value.slice(6, 8))}일`;
}

function formatWindow(conditions: Conditions | SearchDescription | null): string {
  if (!conditions) return "";
  const date = "dep_date" in conditions ? conditions.dep_date : conditions.depDate;
  const start = "dep_time" in conditions ? conditions.dep_time : conditions.depTime;
  const end = "max_dep_time" in conditions ? conditions.max_dep_time : conditions.maxDepTime;
  return `${formatDate(date)} · ${clockFromCompact(start)}–${end === "2400" ? "마지막 열차" : clockFromCompact(end)}`;
}

function favouriteEmptyMark(): string {
  return `<span class="empty-mark" aria-hidden="true"><svg viewBox="0 0 64 64" fill="none"><path d="M15 43h34M19 43l-5 8m31-8 5 8"/><circle cx="17" cy="28" r="6"/><circle cx="47" cy="28" r="6"/><path d="M23 28h18M32 28v-7"/><path class="filled" d="M27 12h10v14l-5-3-5 3z"/></svg></span>`;
}

function activityEmptyMark(): string {
  return `<span class="empty-mark" aria-hidden="true"><svg viewBox="0 0 64 64" fill="none"><rect x="13" y="15" width="38" height="30" rx="9"/><path d="M20 45l-5 7m29-7 5 7M21 26h22M23 36h4m10 0h4"/><circle class="filled" cx="46" cy="17" r="7"/><path class="signal" d="M46 14v3l2 2"/></svg></span>`;
}

export class JariApp {
  private generation = 0;
  private readonly root: HTMLElement;
  private readonly api: MobileApi;
  private readonly options: AppOptions;
  private state: BootstrapState | null = null;
  private view: AppView = "auth";
  private history: AppView[] = [];
  private connection: ConnectionState = "unknown";
  private draft: BookingDraft = conditionsToDraft(null);
  private conditions: Conditions | null = null;
  private selectedTrains: string[] = [];
  private trainOptions: TrainOption[] = [];
  private trainListTruncated = false;
  private seatDialog: SeatDialogState | null = null;
  private cancellationTargets: CancellationWaitPlan["trains"] = [];
  private notifications: NotificationItem[] = [];
  private notificationsLoaded = false;
  private authGate: "choose" | "admin" | "guest" = "choose";
  private authMode: "login" | "register" = "login";
  private invitePreview = "";
  private inviteLoading = false;
  private notifySaveVersion = 0;
  private scheduleOpen = false;
  private accessRequired = false;
  private accessRequestPending = false;
  private busy = false;
  private error = "";
  private toast = "";
  private toastTimer: number | null = null;
  private pollTimer: number | null = null;
  private theme: "light" | "dark";

  constructor(root: HTMLElement, api: MobileApi, options: AppOptions) {
    this.root = root;
    this.api = api;
    this.options = options;
    const savedTheme = window.localStorage.getItem("jari.theme");
    this.theme =
      savedTheme === "light" || savedTheme === "dark"
        ? savedTheme
        : window.matchMedia?.("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light";
    this.onClick = this.onClick.bind(this);
    this.onChange = this.onChange.bind(this);
    this.onSubmit = this.onSubmit.bind(this);
    this.root.addEventListener("click", this.onClick);
    this.root.addEventListener("change", this.onChange);
    this.root.addEventListener("input", this.onChange);
    this.root.addEventListener("submit", this.onSubmit);
  }

  private resetSession(): void {
    this.generation += 1;
    if (this.pollTimer !== null) window.clearInterval(this.pollTimer);
    if (this.toastTimer !== null) window.clearTimeout(this.toastTimer);
    this.pollTimer = null;
    this.toastTimer = null;
    this.state = null;
    this.view = "auth";
    this.history = [];
    this.connection = "unknown";
    this.draft = conditionsToDraft(null);
    this.conditions = null;
    this.selectedTrains = [];
    this.trainOptions = [];
    this.trainListTruncated = false;
    this.seatDialog = null;
    this.inviteLoading = false;
    this.notifySaveVersion += 1;
    this.cancellationTargets = [];
    this.notifications = [];
    this.notificationsLoaded = false;
    this.authGate = "choose";
    this.authMode = "login";
    this.invitePreview = "";
    this.scheduleOpen = false;
    this.accessRequired = false;
    this.accessRequestPending = false;
    this.busy = false;
    this.error = "";
    this.toast = "";
    // Remove old form values before rendering can preserve any editable controls.
    this.root.replaceChildren();
  }

  async start(authenticated: boolean): Promise<void> {
    this.resetSession();
    if (!authenticated && !this.options.demoMode) {
      this.view = "auth";
      this.connection = "unknown";
      this.render();
      return;
    }
    this.view = "home";
    await this.reload();
  }

  dispose(): void {
    this.resetSession();
    this.root.removeEventListener("click", this.onClick);
    this.root.removeEventListener("change", this.onChange);
    this.root.removeEventListener("input", this.onChange);
    this.root.removeEventListener("submit", this.onSubmit);
    if (this.pollTimer !== null) window.clearInterval(this.pollTimer);
    if (this.toastTimer !== null) window.clearTimeout(this.toastTimer);
  }

  navigate(next: AppView): void {
    if (next === this.view) return;
    this.syncJourneyDraft();
    if (this.view !== "auth") this.history.push(this.view);
    this.view = next;
    this.error = "";
    this.render();
    window.scrollTo({ top: 0, behavior: "instant" });
    const screen = this.root.querySelector<HTMLElement>(".screen");
    if (screen) screen.scrollTop = 0;
    if (next === "notifications") void this.loadNotifications();
  }

  back(): boolean {
    this.syncJourneyDraft();
    const previous = this.history.pop();
    if (!previous) return false;
    this.view = previous;
    this.error = "";
    this.render();
    const screen = this.root.querySelector<HTMLElement>(".screen");
    if (screen) screen.scrollTop = 0;
    return true;
  }

  notify(message: string): void {
    this.showToast(message);
    this.render();
  }

  private async reload(): Promise<void> {
    const generation = this.generation;
    const hadState = this.state !== null;
    this.busy = true;
    this.error = "";
    this.render();
    try {
      const state = await this.api.bootstrap();
      if (generation !== this.generation) return;
      state.capabilities = normalizeCapabilities(state.capabilities);
      this.state = state;
      this.connection = "online";
      if (!hadState) {
        this.draft = conditionsToDraft(state.draft);
        this.conditions = state.draft;
        this.selectedTrains = state.draft?.trains?.map(String) ?? [];
        this.cancellationTargets = state.draft?.seat_plan?.trains ?? [];
      }
      await this.options.onBootstrap?.(state);
      if (generation !== this.generation) return;
      this.startPolling();
    } catch (error) {
      if (generation === this.generation) this.handleError(error);
    } finally {
      if (generation === this.generation) {
        this.busy = false;
        this.render();
      }
    }
  }

  private startPolling(): void {
    if (this.pollTimer !== null) window.clearInterval(this.pollTimer);
    this.pollTimer = window.setInterval(() => void this.pollStatus(), 30_000);
  }

  private async pollStatus(): Promise<void> {
    const generation = this.generation;
    if (!this.state || this.view === "auth") return;
    try {
      const status = await this.api.status();
      if (generation !== this.generation) return;
      this.state.running = status.running;
      this.state.scheduled = status.scheduled;
      this.state.pending = status.pending;
      this.connection = "online";
      if (this.view === "home" || this.view === "activity") this.render();
    } catch (error) {
      if (generation !== this.generation || (error instanceof ApiError && error.kind === "stale")) return;
      if (error instanceof ApiError && error.kind === "auth") {
        this.handleError(error);
        return;
      }
      // A server that answered with an error is reachable; only the state is unconfirmed.
      this.connection = error instanceof ApiError && error.kind === "server" ? "unknown" : "offline";
      if (this.view === "home" || this.view === "activity") {
        this.render();
      }
    }
  }

  private render(preserveJourney = true): void {
    if (preserveJourney) this.syncJourneyDraft();
    // Preserve controls outside the journey model during incidental rerenders.
    // Session reset removes their source DOM, so they cannot cross accounts.
    const fields = Array.from(this.root.querySelectorAll<HTMLInputElement | HTMLSelectElement>("input, select"))
      .filter((field) => !field.closest("#conditions-form"))
      .map((field) => ({ id: field.id, name: field.name, form: field.form?.id, value: field.value, checked: field instanceof HTMLInputElement && field.checked }));
    const restoreFields = () => {
      for (const field of this.root.querySelectorAll<HTMLInputElement | HTMLSelectElement>("input, select")) {
        const old = fields.find((item) => item.id === field.id && item.name === field.name && item.form === field.form?.id);
        if (!old) continue;
        field.value = old.value;
        if (field instanceof HTMLInputElement) field.checked = old.checked;
      }
    };
    if (document.documentElement.dataset.theme !== this.theme) void this.options.onTheme?.(this.theme);
    document.documentElement.dataset.theme = this.theme;
    document.documentElement.style.colorScheme = this.theme;
    if (this.view === "auth") {
      this.root.innerHTML = this.renderAuth();
      restoreFields();
      return;
    }
    const content = this.state ? this.renderView() : this.renderUnavailable();
    this.root.innerHTML = `
      <div class="app-shell">
        ${this.options.demoMode ? '<aside class="demo-banner" data-demo-banner aria-label="데모 상태"><b>데모 모드</b><span>샘플 데이터 · 실제 조회·예약 없음</span></aside>' : ""}
        ${this.connection === "offline" ? '<aside class="offline-banner" aria-label="연결 상태">오프라인 · 마지막으로 받은 상태를 보여드려요</aside>' : ""}
        <header class="topbar">
          <button class="brand" data-view="home" aria-label="자리났다 홈"><span class="brand-mark" aria-hidden="true"><svg viewBox="0 0 32 32"><rect x="5" y="7" width="9" height="14" rx="3"></rect><rect x="18" y="7" width="9" height="14" rx="3"></rect><path d="M7 25h18"></path></svg></span><span><b>자리났다</b><small>내 여행의 빈자리</small></span></button>
          <button class="header-action notification-button" data-view="notifications" type="button" aria-label="알림 열기" title="알림"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"></path><path d="M10 21h4"></path></svg></button>
        </header>
        <main class="screen screen-${this.view}">${content}</main>
        ${this.renderNavigation()}
        ${this.renderSeatDialog()}
        ${this.busy ? '<div class="blocker" role="status"><span class="spinner"></span><b>잠시만 기다려 주세요</b></div>' : ""}
        <div class="toast" role="status" aria-live="polite">${escapeHtml(this.toast)}</div>
      </div>`;
    restoreFields();
  }

  private renderView(): string {
    switch (this.view) {
      case "home":
        return this.renderHome();
      case "journey":
        return this.renderJourney();
      case "trains":
        return this.renderTrains();
      case "confirm":
        return this.renderConfirm();
      case "activity":
        return this.renderActivity();
      case "favourites":
        return this.renderFavourites();
      case "notifications":
        return this.renderNotifications();
      case "settings":
        return this.renderSettings();
      case "rail-account":
        return this.renderRailAccount();
      default:
        return this.renderHome();
    }
  }

  private renderHome(): string {
    const state = this.state!;
    const radar = deriveRadarView({
      running: this.radarRunning(state),
      connection: this.connection,
    });
    const paymentFirst = state.pending.length > 0;
    const journey = paymentFirst ? null : state.running ?? state.scheduled?.search ?? null;
    const route = journey
      ? `<div class="route-title"><strong>${escapeHtml(journey.srcLocate)}</strong><span>→</span><strong>${escapeHtml(journey.dstLocate)}</strong></div>
         <p class="route-meta">${escapeHtml(formatWindow(journey))}</p>`
      : "";
    const seatLabels: Record<string, string> = { GENERAL_FIRST: "일반실 우선", GENERAL_ONLY: "일반실만", SPECIAL_FIRST: "특실 우선", SPECIAL_ONLY: "특실만" };
    const status = this.connection === "online"
      ? state.pending.length
        ? { kind: "reserved", title: "빈자리를 찾았어요", description: "아래 예약 내역에서 결제 기한을 확인하세요." }
        : !state.running && state.scheduled
          ? { kind: "scheduled", title: "예약한 시각에 검색을 시작해요", description: `${formatStamp(state.scheduled.startAt)} 시작 예정` }
          : radar
      : radar;
    return `<div class="home-layout"><div class="home-primary">
      <section class="status-heading">
        <div><p class="eyebrow">검색 현황</p><h1>내 검색 현황</h1></div>
      </section>
      <section class="card search-status-card" data-search-status data-state="${status.kind}" aria-label="검색 상태 요약">
        <div class="search-status-title"><span class="search-status-icon">${status.kind === "reserved" ? "예약" : ["error", "stale", "offline"].includes(status.kind) ? "확인" : "대기"}</span><h2>${escapeHtml(status.title)}</h2></div>
        ${route}
        ${journey ? `<p class="search-conditions">${escapeHtml(journey.trainTypeShow)} · ${escapeHtml(seatLabels[journey.specialInfoShow] ?? journey.specialInfoShow)} · ${journey.passengerCount}명</p>` : ""}
        <p class="search-status-description">${escapeHtml(status.description)}</p>
        ${!paymentFirst && state.running && radar.lastCheckedLabel ? `<p class="search-last-check">마지막 조회 ${escapeHtml(radar.lastCheckedLabel)}</p>` : ""}
        ${state.running || state.scheduled || state.pending.length ? `<button class="button secondary" data-view="activity">${paymentFirst ? "예약 확인하기" : "검색 상세 보기"}<span>→</span></button>` : ""}
      </section>
      ${this.renderPendingCard(true)}
      ${state.scheduled ? this.renderScheduledCard() : ""}
      <button class="button primary roomy" data-action="new-journey" ${state.capabilities.korail ? "" : "disabled"}><span>${state.capabilities.korail ? "새 여정 찾기" : "예약 서버에 연결해 주세요"}</span><b>＋</b></button>
      </div><aside class="home-secondary">
      <section class="section-head"><div><p class="eyebrow">빠른 실행</p><h2>자주 가는 구간</h2></div><button class="text-button" data-view="favourites">전체 보기</button></section>
      <div class="route-list">${state.favourites.length ? state.favourites.slice(0, 2).map((favourite) => this.renderFavouriteRow(favourite, true)).join("") : '<div class="empty compact"><p>아직 저장한 구간이 없어요.</p></div>'}</div>
      </aside></div>`;
  }

  private renderPendingCard(compact: boolean): string {
    const pending = this.state!.pending;
    if (!pending.length) return "";
    const total = this.state!.running?.passengerCount ?? pending.length;
    const progress = total > pending.length ? ` · ${pending.length}/${total}석 확보` : "";
    return `<section class="card payment-card ${compact ? "compact-card" : ""}">
      <div class="card-label warning"><i></i>결제가 필요한 예약 ${pending.length}건${progress}</div>
      ${pending.map((item) => `<div class="payment-row"><div><strong>${escapeHtml(item.trainInfo)}</strong><small>${item.seatClass ? `${item.seatClass === "general" ? "일반실" : "특실"} · ` : ""}${item.seatLabels?.length ? `${escapeHtml(item.seatLabels.join(", "))} · ` : item.seatNumber !== null ? `좌석 ${escapeHtml(item.seatNumber)} · ` : ""}${item.reservationId ? `예약번호 ${escapeHtml(item.reservationId)}` : "예약번호 정보 없음"}</small></div><b>${item.expiresAt ? `${escapeHtml(formatStamp(item.expiresAt))}까지` : "결제 기한 정보 없음"}</b></div>`).join("")}
      <a class="button primary" href="${escapeHtml(this.state!.paymentUrl)}" target="_blank" rel="noreferrer">코레일에서 결제하기 <span>↗</span></a>
      ${compact ? '<button class="text-button danger" data-view="activity">예약 관리</button>' : '<button class="button ghost danger" data-action="cancel-pending">예약 전체 취소</button>'}
    </section>`;
  }

  private renderScheduledCard(): string {
    const scheduled = this.state!.scheduled!;
    return `<section class="card scheduled-card"><div class="card-label"><i></i>예약된 검색</div><div class="route-line"><strong>${escapeHtml(scheduled.search.srcLocate)} → ${escapeHtml(scheduled.search.dstLocate)}</strong><span>${escapeHtml(formatStamp(scheduled.startAt))} 시작</span></div><button class="text-button danger" data-action="cancel-search">검색 예약 취소</button></section>`;
  }

  private renderJourney(): string {
    const state = this.state!;
    const capabilities = state.capabilities;
    const stationOptions = state.rail.stations
      .map((station) => `<option value="${escapeHtml(station)}"></option>`)
      .join("");
    return `${this.renderSubhead("새 여정", "여행 조건을 알려 주세요")}
      <form id="conditions-form" class="form-stack">
        <section class="card form-card">
          <div class="form-section-head"><div><span>01</span><h2>어디로 떠나세요?</h2></div><button class="text-button" type="button" data-action="swap">출발·도착 바꾸기 ⇄</button></div>
          <div class="station-grid">
            <label class="field"><span>출발역</span><input name="src_station" list="stations" value="${escapeHtml(this.draft.srcStation)}" required autocomplete="off"></label>
            <span class="route-arrow">→</span>
            <label class="field"><span>도착역</span><input name="dst_station" list="stations" value="${escapeHtml(this.draft.dstStation)}" required autocomplete="off"></label>
          </div>
          <datalist id="stations">${stationOptions}</datalist>
          <fieldset class="choice-grid"><legend>열차 종류</legend>
            ${this.radioCard("train_type", "1", "KTX 계열", "KTX·KTX-산천", this.draft.trainType === "1")}
            ${this.radioCard("train_type", "2", "모든 열차", "무궁화호 포함", this.draft.trainType === "2")}
          </fieldset>
          <div class="notice rail-merge-note"><b>수서 출발 열차도 함께 찾아요.</b><p>기존 SRT 노선은 KTX로 통합됐어요. 코레일 계정 하나로 검색하고 예약할 수 있어요.</p></div>
        </section>
        <section class="card form-card">
          <div class="form-section-head"><div><span>02</span><h2>언제 떠나세요?</h2></div></div>
          <label class="field"><span>출발 날짜</span><input name="dep_date" type="date" value="${escapeHtml(this.draft.depDate)}" required></label>
          <div class="two-columns">
            <label class="field"><span>이 시간부터</span><input name="dep_time" type="time" value="${escapeHtml(this.draft.depTime)}" required></label>
            <label class="field"><span>이 시간까지</span><input name="max_dep_time" type="time" value="${escapeHtml(this.draft.maxDepTime)}" ${this.draft.unlimitedTime ? "disabled" : "required"}></label>
          </div>
          <label class="check-row"><input name="unlimited_time" type="checkbox" ${this.draft.unlimitedTime ? "checked" : ""}><span><b>마지막 열차까지 찾기</b><small>자정 전 마지막 열차까지 검색해요</small></span></label>
        </section>
        <section class="card form-card">
          <div class="form-section-head"><div><span>03</span><h2>어떤 좌석이 좋으세요?</h2></div></div>
          <fieldset class="grade-picker"><legend>좌석 등급</legend>
            <label class="check-row grade-any"><input type="radio" name="seat_grade_mode" value="any" ${this.draft.seatGradeMode !== "specific" ? "checked" : ""}><span><b>좌석 등급 상관없음</b><small>가능한 좌석을 가장 빠르게 예약해요</small></span></label>
            <label class="check-row"><input type="radio" name="seat_grade_mode" value="specific" ${this.draft.seatGradeMode === "specific" ? "checked" : ""}><span><b>좌석 등급 지정</b><small>열차 조회 후 실제 좌석표에서 자리를 골라요</small></span></label>
          </fieldset>
          ${this.draft.seatGradeMode === "specific" ? `<fieldset class="choice-grid seat-class-picker"><legend>찾을 좌석 등급</legend>
            <label class="choice-card"><input type="checkbox" name="seat_class" value="general" ${(this.draft.seatClasses ?? []).includes("general") ? "checked" : ""}><span><b>일반실</b><small>일반 좌석</small></span></label>
            <label class="choice-card"><input type="checkbox" name="seat_class" value="special" ${(this.draft.seatClasses ?? []).includes("special") ? "checked" : ""}><span><b>특실</b><small>넓은 좌석</small></span></label>
          </fieldset><div class="notice calm seat-detail-note"><b>세부 좌석은 다음 단계에서 골라요.</b><p>열차별 실제 좌석표에서 맨 앞·맨 뒤·4인 동반석·원하는 자리를 선택할 수 있어요.</p></div>` : ""}
          <div class="passenger-row"><span><b>인원</b><small>최대 9명</small></span><div class="stepper"><button type="button" data-action="passenger-minus">−</button><output>${this.draft.passengerCount}명</output><button type="button" data-action="passenger-plus">＋</button></div></div>
          ${this.draft.passengerCount > 1 ? `<fieldset class="choice-grid"><legend>좌석 배치</legend>${this.radioCard("seat_strategy", "1", "연속 좌석", "같은 열차·호차에서 붙은 자리만", this.draft.seatStrategy === "1")}${this.radioCard("seat_strategy", "2", "따로 앉아도 괜찮아요", "한 자리만 잡혀도 바로 알려드려요", this.draft.seatStrategy === "2")}</fieldset>` : ""}
        </section>
        ${this.renderError()}
        ${capabilities.korail ? "" : '<p class="availability center">예약 서버가 연결되지 않아 현재 앱 계정 기능만 사용할 수 있어요.</p>'}
        <button class="button primary" type="submit" ${capabilities.korail ? "" : "disabled"}>열차 조회하기 <span>→</span></button>
      </form>`;
  }

  private radioCard(
    name: string,
    value: string,
    title: string,
    hint: string,
    checked: boolean,
  ): string {
    return `<label class="choice-card"><input type="radio" name="${name}" value="${value}" ${checked ? "checked" : ""}><span><b>${title}</b><small>${hint}</small></span></label>`;
  }

  private renderTrains(): string {
    const conditions = this.conditions ?? buildConditions(this.draft);
    const list = this.trainOptions.length
      ? this.trainOptions
          .map((train) => {
            const departure = train.dep_time ? clockFromCompact(train.dep_time) : "";
            const arrival = train.arr_time ? clockFromCompact(train.arr_time) : "";
            const configuredClasses = conditions.seat_classes?.length
              ? conditions.seat_classes
              : (["general", "special"] as SeatClass[]);
            // "좌석 지정": pick seats on the map. Pressing the card itself takes the train as a whole - any seat will do.
            const seatButton = (seatClass: SeatClass, available: boolean | undefined) => {
              const label = seatClass === "general" ? "일반실" : "특실";
              const selectedCount = this.cancellationTargets
                .find((target) => target.trainNo === train.no && target.seatClass === seatClass)
                ?.targets.length ?? 0;
              return `<button class="button seat-action ${selectedCount ? "selected" : ""}" type="button" data-seat-map="${escapeHtml(train.trainKey || "")}" data-train-no="${escapeHtml(train.no)}" data-seat-class="${seatClass}" data-seat-mode="${available ? "immediate" : "wait"}" ${train.trainKey ? "" : "disabled"}><span>${available ? `${label} 좌석 선택` : `${label} 좌석 지정`}</span>${selectedCount ? `<em>선택 완료 · ${selectedCount}석</em>` : ""}</button>`;
            };
            const classButtons = configuredClasses.map((seatClass) =>
              seatButton(seatClass, seatClass === "general" ? train.generalAvailable : train.specialAvailable),
            ).join("");
            const anyAvailable = train.generalAvailable || train.specialAvailable;
            const anyAction = this.draft.seatGradeMode === "specific"
              ? classButtons
              : anyAvailable
                ? `<button class="button seat-action" type="button" data-immediate-any="${escapeHtml(train.no)}">바로 예약</button>`
                : (["general", "special"] as SeatClass[]).map((seatClass) => seatButton(seatClass, false)).join("");
            const official = train.waitlistEligible
              ? `<button class="text-button" type="button" data-official-waitlist="${escapeHtml(train.no)}">코레일 예약 대기</button>`
              : "";
            const targets = this.cancellationTargets.filter((target) => target.trainNo === train.no);
            const selectedCount = targets.reduce((sum, target) => sum + target.targets.length, 0);
            const wholeTrain = targets.some((target) => !target.targets.length);
            return `<article class="train-card ${targets.length ? "selected" : ""}"><button type="button" class="train-main" data-train-toggle="${escapeHtml(train.no)}" aria-pressed="${wholeTrain}"><small>${escapeHtml(train.name || `열차 ${train.no}`)}</small><b>${departure && arrival ? `${escapeHtml(departure)} <i>→</i> ${escapeHtml(arrival)}` : escapeHtml(train.label)}</b><em>${escapeHtml(train.label)}</em></button><span class="seat-badge ${wholeTrain ? "chosen" : anyAvailable ? "available" : "soldout"}">${wholeTrain ? "좌석 무관 선택" : selectedCount ? `${selectedCount}석 지정` : anyAvailable ? "좌석 있음" : "매진"}</span><div class="train-actions">${anyAction}${official}</div></article>`;
          })
          .join("")
      : '<div class="empty"><span>⌁</span><h2>조회된 열차가 없어요</h2><p>시간이나 구간을 바꿔 다시 조회해 주세요.</p></div>';
    return `${this.renderSubhead("열차 선택", "좌석 예약·취소표 대기")}
      <div class="context-line"><b>${escapeHtml(conditions.src_station)} → ${escapeHtml(conditions.dst_station)}</b><span>${escapeHtml(formatWindow(conditions))}</span></div>
      <p class="intro">좌석이 있으면 바로 예약할 수 있어요. 매진이면 원하는 좌석 범위를 골라 취소표 대기를 시작하세요.</p>
      ${this.trainListTruncated ? '<div class="notice warning">목록이 길어 일부 열차만 보여드려요. 시간대 전체 검색은 그대로 이용할 수 있어요.</div>' : ""}
      <div class="train-list">${list}</div>
      ${this.renderError()}
      ${this.renderAccessRequest()}
      <button class="button primary sticky-action" data-action="trains-next">${this.cancellationTargets.length ? `${new Set(this.cancellationTargets.map((target) => target.trainNo)).size}편 선택 · 조건 확인` : "시간대 전체 · 조건 확인"} <span>→</span></button>`;
  }

  private renderSeatDialog(): string {
    const dialog = this.seatDialog;
    if (!dialog) return "";
    const classLabel = dialog.seatClass === "general" ? "일반실" : "특실";
    const title = dialog.mode === "immediate"
      ? "예약할 좌석을 선택해 주세요"
      : "좌석 범위를 선택해 주세요";
    // Scanning dialog.selected per car tab and per cell is O(n) each; with a few hundred seats selected that adds up over a whole render. Index it once instead.
    const selectedByCar = new Map<number, number>();
    const selectedKeys = new Set<string>();
    for (const seat of dialog.selected) {
      selectedByCar.set(seat.carNo, (selectedByCar.get(seat.carNo) ?? 0) + 1);
      selectedKeys.add(`${seat.carNo}:${seat.seatNo}`);
    }
    const locked = dialog.bulkApplying;
    // The cars "호차 앞뒤 제외" will leave out, shown as such the moment the count changes rather than only after "모든 호차에 적용".
    const kept = keptCarNos(dialog);
    const cars = dialog.cars.map((car) => {
      const count = selectedByCar.get(car.carNo) ?? 0;
      const trimmed = !kept.has(car.carNo);
      return `<button type="button" class="car-tab ${dialog.carNo === car.carNo ? "selected" : ""} ${count ? "picked" : ""} ${trimmed ? "trimmed" : ""}" data-seat-car="${escapeHtml(car.carNo)}" ${locked ? "disabled" : ""}><b>${escapeHtml(car.carNo)}호차</b><small>${trimmed ? "제외" : dialog.layoutReference ? "좌석표" : `${escapeHtml(car.remainingSeatCount)}석 가능`}</small>${count ? `<em>${escapeHtml(count)}</em>` : ""}</button>`;
    }).join("");
    const selectable = (seat: SeatMapSeat) => this.seatSelectable(dialog, seat);
    const layout = dialog.inventory ? groupSeatsByLayout(dialog.inventory.seats) : [];
    // Window columns read from the layout once per car; a per-cell "first/last in this row" check mislabels the aisle seat next to a short row (door, wheelchair space) as a window seat.
    const windowColumns = dialog.inventory ? new Set(seatColumnSets(dialog.inventory.seats).window) : new Set<string>();
    const aisleBefore = (row: SeatMapSeat[], index: number) => {
      const [previous, seat] = [row[index - 1], row[index]];
      return Boolean(previous?.adjacencyGroup && seat?.adjacencyGroup && previous.adjacencyGroup !== seat.adjacencyGroup);
    };
    const rows = dialog.inventory
      ? layout.map((row) => {
          const cells = row.map((seat, index) => {
            const groupChanged = aisleBefore(row, index);
            const selected = selectedKeys.has(`${seat.carNo}:${seat.seatNo}`);
            const description = [seat.label, seat.direction, seat.floor, seat.familyLabel].filter(Boolean).join(" · ");
            const available = this.seatAvailable(dialog, seat);
            const windowSeat = seat.column ? windowColumns.has(seat.column) : index === 0 || index === row.length - 1;
            // A numeric-label car (무궁화 등) has a synthetic column letter; show the real seat number instead.
            const cellText = seat.column && !isNumericSeatLabel(seat) ? seat.column : seat.label;
            return `${groupChanged ? '<span class="train-aisle" aria-hidden="true">통로</span>' : ""}<button type="button" class="seat-cell ${available ? "available" : "occupied"} ${selected ? "selected" : ""} ${windowSeat ? "window-seat" : ""}" data-seat-no="${escapeHtml(seat.seatNo)}" aria-pressed="${selected}" ${selectable(seat) && !locked ? "" : "disabled"} title="${escapeHtml(description)}"><b>${escapeHtml(cellText)}</b><small>${seat.familyLabel ? "가족" : windowSeat ? "창가" : available ? "가능" : "대기"}</small></button>`;
          }).join("");
          return `<div class="seat-row"><span>${escapeHtml(row[0]?.row ?? "")}</span><div class="seat-row-track">${cells}</div></div>`;
        }).join("") || (dialog.inventory.seats.length
          ? '<div class="seat-dialog-error" role="alert"><b>이 열차의 좌석표 형식을 아직 읽지 못했어요.</b><p>다른 호차를 선택하거나 나중에 다시 시도해 주세요.</p></div>'
          : "")
      : dialog.error
        ? `<div class="seat-dialog-error" role="alert"><b>좌석표를 불러오지 못했어요.</b><p>${escapeHtml(dialog.error)}</p><button type="button" class="button seat-action" data-action="retry-seat-dialog">다시 시도</button></div>`
        : '<div class="seat-loading"><span class="spinner"></span><p>실제 좌석표를 불러오고 있어요.</p></div>';
    const selectedLabels = dialog.selected.length > 6
      ? `${new Set(dialog.selected.map((seat) => seat.carNo)).size}개 호차 · ${dialog.selected.length}석`
      : dialog.selected.map((seat) => `${seat.carNo}호차 ${seat.label}`).join(", ");
    const passengerCount = this.draft.passengerCount;
    const confirmText = dialog.mode === "immediate"
      ? `${dialog.selected.length}/${passengerCount}석 선택 · 예약하기`
      : `${dialog.selected.length}석 범위로 취소표 대기`;
    return `<div class="modal-backdrop" role="presentation"><section class="seat-dialog" role="dialog" aria-modal="true" aria-labelledby="seat-dialog-title">
      <header><div><p class="eyebrow">${escapeHtml(`${dialog.train.name || "열차"} ${dialog.train.no}`)} · ${classLabel}</p><h2 id="seat-dialog-title">${title}</h2></div><button type="button" class="icon-button" data-action="close-seat-dialog" aria-label="좌석 선택 닫기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg></button></header>
      <div class="car-tabs" aria-label="호차 선택">${cars}</div>
      ${dialog.inventory ? this.renderSeatFilter(dialog, dialog.inventory.seats) : ""}
      <div class="seat-legend">${dialog.layoutReference ? "" : '<span><i class="available"></i>현재 예약 가능</span>'}<span><i class="occupied"></i>${dialog.mode === "wait" ? "취소표 대기 가능" : "선택 불가"}</span><span><i class="selected"></i>선택</span></div>
      <div class="seat-map-live">${rows}</div>
      ${dialog.inventory && dialog.error ? `<p class="notice warning seat-inline-error" role="alert">${escapeHtml(dialog.error)}</p>` : ""}
      <footer><div class="seat-selection"><p>${selectedLabels ? escapeHtml(selectedLabels) : "선택한 좌석이 없어요."}</p>${dialog.selected.length || dialog.filter.columns.length || dialog.filter.trimRows || dialog.filter.excludeFamily ? '<button type="button" class="seat-filter-clear" data-seat-filter="clear">선택 해제</button>' : ""}</div><button type="button" class="button primary" data-action="confirm-seat-dialog" ${locked ? "disabled" : ""}>${confirmText}</button></footer>
    </section></div>`;
  }

  private renderSeatFilter(dialog: SeatDialogState, seats: SeatMapSeat[]): string {
    const { filter } = dialog;
    const sets = seatColumnSets(seats);
    const locked = dialog.bulkApplying;
    // A numeric-label car (무궁화 등) synthesizes A–D columns just to lay seats out; the letters aren't real, so don't offer them as a filter.
    // Judge the whole car by its placed seats (row !== null) with `every`: one unreadable seat in an otherwise normal KTX car shouldn't hide every column chip.
    const placedSeats = seats.filter((seat) => seat.row !== null);
    const numericLabels = placedSeats.length > 0 && placedSeats.every((seat) => isNumericSeatLabel(seat));
    const chip = (action: string, label: string, on: boolean) =>
      `<button type="button" class="seat-chip ${on ? "selected" : ""}" data-seat-filter="${escapeHtml(action)}" aria-pressed="${on}" ${locked ? "disabled" : ""}>${escapeHtml(label)}</button>`;
    const allOn = (columns: string[]) => columns.length > 0 && columns.every((column) => filter.columns.includes(column));
    const columnChips = numericLabels ? "" : sets.columns.map((column) => chip(`col:${column}`, column, filter.columns.includes(column))).join("");
    const trimLabel = filter.trimRows ? `${filter.trimRows}줄` : "없음";
    const family = seats.some((seat) => seat.familyLabel)
      ? `<button type="button" role="switch" class="seat-switch" data-seat-filter="family" aria-checked="${filter.excludeFamily}" ${locked ? "disabled" : ""}><span aria-hidden="true"></span>가족석 제외</button>`
      : "";
    // Both steppers on one row, each captioned underneath, so "좌석 앞뒤 제외 1줄" and "호차 앞뒤 제외 1개" read the same way.
    const stepper = (key: string, caption: string, value: string, atMin: boolean, atMax: boolean) =>
      `<div class="seat-stepper-block"><div class="seat-stepper"><button type="button" data-seat-filter="${key}:-" aria-label="${caption} 줄이기" ${atMin || locked ? "disabled" : ""}>−</button><output>${value}</output><button type="button" data-seat-filter="${key}:+" aria-label="${caption} 늘리기" ${atMax || locked ? "disabled" : ""}>+</button></div><small>${caption}</small></div>`;
    const multiCar = dialog.mode === "wait" && dialog.cars.length > 1;
    const carStepper = multiCar
      ? stepper("cars", "호차 앞뒤 제외", dialog.trimCars ? `${dialog.trimCars}개` : "없음", dialog.trimCars <= 0, dialog.trimCars >= maxTrimCars(dialog.cars.length))
      : "";
    const applyAll = multiCar
      ? `<button type="button" class="button ghost seat-apply-all" data-action="apply-all-cars" ${locked ? "disabled" : ""}>${locked ? "모든 호차 확인 중…" : "모든 호차에 적용"}</button>`
      : "";
    return `<div class="seat-filter">
      <div class="seat-filter-row"><span>열</span><div class="seat-chips">${columnChips}<i aria-hidden="true"></i>${chip("pair:window", "창가", allOn(sets.window))}${sets.aisle.length ? chip("pair:aisle", "복도", allOn(sets.aisle)) : ""}</div></div>
      <div class="seat-filter-row seat-steppers">${stepper("trim", "좌석 앞뒤 제외", trimLabel, filter.trimRows <= 0, filter.trimRows >= maxTrimRows(seats))}${carStepper}${family}</div>
      ${applyAll}
    </div>`;
  }

  private async openSeatMap(trainKey: string, trainNo: string, seatClass: SeatClass, mode: SeatSelectionMode): Promise<void> {
    const train = this.trainOptions.find((item) => item.no === trainNo);
    if (!train || !trainKey) return;
    const dialog: SeatDialogState = {
      train, seatClass, mode, cars: [], inventory: null, inventories: new Map(), carNo: null,
      selected: [], filter: emptySeatFilter(), layoutReference: false, loading: true, error: "", bulkApplying: false, trimCars: 0, appliedToAll: false,
    };
    this.seatDialog = dialog;
    this.render();
    await this.fetchSeatDialog(dialog);
  }

  private async fetchSeatDialog(dialog: SeatDialogState): Promise<void> {
    const generation = this.generation;
    dialog.loading = true;
    dialog.error = "";
    dialog.cars = [];
    dialog.inventory = null;
    dialog.inventories = new Map();
    dialog.carNo = null;
    dialog.filter = emptySeatFilter();
    dialog.appliedToAll = false;
    this.refreshSeatDialog();
    try {
      const trainKey = dialog.train.trainKey!;
      const result = await this.api.seatCars(trainKey, dialog.seatClass, this.draft.passengerCount);
      if (generation !== this.generation || this.seatDialog !== dialog) return;
      if (!result.cars.length) throw new Error(`${dialog.seatClass === "general" ? "일반실" : "특실"} 좌석표가 아직 제공되지 않아요.`);
      dialog.cars = result.cars;
      dialog.carNo = result.cars[0]!.carNo;
      dialog.layoutReference = Boolean(result.layoutReference);
      this.refreshSeatDialog();
      const inventory = await this.api.seatInventory(trainKey, dialog.carNo, dialog.seatClass, this.draft.passengerCount);
      if (generation !== this.generation || this.seatDialog !== dialog) return;
      dialog.inventory = inventory;
      dialog.inventories.set(dialog.carNo, inventory);
      dialog.layoutReference ||= Boolean(inventory.layoutReference);
      dialog.loading = false;
      this.refreshSeatDialog();
    } catch (error) {
      if (generation !== this.generation || this.seatDialog !== dialog) return;
      dialog.loading = false;
      dialog.error = error instanceof Error && error.message && !error.message.includes("layout unavailable")
        ? error.message
        : "잠시 후 다시 시도해 주세요.";
      this.refreshSeatDialog();
    }
  }

  private refreshSeatDialog(preservePosition = false): void {
    const current = this.root.querySelector<HTMLElement>(".modal-backdrop");
    if (!current) {
      this.render();
      return;
    }
    const seatMap = current.querySelector<HTMLElement>(".seat-map-live");
    const carTabs = current.querySelector<HTMLElement>(".car-tabs");
    const scrollTop = preservePosition ? seatMap?.scrollTop ?? 0 : 0;
    const tabsScrollLeft = carTabs?.scrollLeft ?? 0;
    const template = document.createElement("template");
    template.innerHTML = this.renderSeatDialog();
    const replacement = template.content.firstElementChild;
    if (!replacement) return;
    current.replaceWith(replacement);
    const nextMap = replacement.querySelector<HTMLElement>(".seat-map-live");
    const nextTabs = replacement.querySelector<HTMLElement>(".car-tabs");
    if (nextMap) nextMap.scrollTop = scrollTop;
    if (nextTabs) nextTabs.scrollLeft = tabsScrollLeft;
  }

  private async retrySeatDialog(): Promise<void> {
    const dialog = this.seatDialog;
    if (dialog) await this.fetchSeatDialog(dialog);
  }

  private async loadSeatCar(carNo: number): Promise<void> {
    const dialog = this.seatDialog;
    if (!dialog || dialog.carNo === carNo || dialog.bulkApplying) return;
    const generation = this.generation;
    dialog.carNo = carNo;
    // Conditions describe the car they were applied to; chips left on over another car would read as applied when they are not.
    // After "모든 호차에 적용" they describe every car, so they stay on.
    if (!dialog.appliedToAll) dialog.filter = emptySeatFilter();
    dialog.error = "";
    const cached = dialog.inventories.get(carNo);
    if (cached) {
      dialog.inventory = cached;
      dialog.layoutReference ||= Boolean(cached.layoutReference);
      dialog.loading = false;
      this.refreshSeatDialog();
      return;
    }
    dialog.inventory = null;
    dialog.loading = true;
    this.refreshSeatDialog();
    try {
      const inventory = await this.api.seatInventory(dialog.train.trainKey!, carNo, dialog.seatClass, this.draft.passengerCount);
      if (generation !== this.generation || this.seatDialog !== dialog) return;
      dialog.inventory = inventory;
      dialog.inventories.set(carNo, inventory);
      dialog.layoutReference ||= Boolean(inventory.layoutReference);
      dialog.loading = false;
      this.refreshSeatDialog();
    } catch {
      if (generation !== this.generation || this.seatDialog !== dialog) return;
      dialog.loading = false;
      dialog.error = "이 호차의 좌석표를 불러오지 못했어요. 다시 시도해 주세요.";
      this.refreshSeatDialog();
    }
  }

  /**
   * WAIT mode only: one request for every car's inventory, then reuses the current filter/selection to pick each car's seats.
   * A per-car loop used to call seatInventory once per car, but each call is a fresh Korail login on the server, and the
   * server's rate limit (10 calls/60s per user) means an 18-car KTX would reliably hit 429 partway through.
   * Locked for the whole run (render disables seat cells/filter/tabs/confirm; toggleSeat/changeSeatFilter/loadSeatCar/confirmSeatDialog
   * bail out early too) so an edit mid-fetch can't be silently overwritten when this replaces dialog.selected at the end.
   */
  private async applyAllCars(): Promise<void> {
    const dialog = this.seatDialog;
    if (!dialog || dialog.mode !== "wait" || dialog.bulkApplying || !dialog.inventory) return;
    const currentCarNo = dialog.inventory.carNo;
    // Snapshot the filter and the current car's selection once, up front, so every other car is judged by the same condition
    // the user tapped with, not by whatever dialog.filter happens to hold by the time the request resolves.
    const filterSnapshot: SeatFilter = { ...dialog.filter };
    const filterEmpty = !filterSnapshot.columns.length && !filterSnapshot.trimRows && !filterSnapshot.excludeFamily;
    const currentLabels = new Set(dialog.selected.filter((seat) => seat.carNo === currentCarNo).map((seat) => seat.label));
    if (filterEmpty && !currentLabels.size) {
      dialog.error = "먼저 이 호차에서 좌석이나 조건을 골라 주세요.";
      this.refreshSeatDialog(true);
      return;
    }
    const generation = this.generation;
    const stale = () => generation !== this.generation || this.seatDialog !== dialog;
    dialog.error = "";
    dialog.bulkApplying = true;
    this.refreshSeatDialog(true);
    try {
      let result: SeatInventoriesResult;
      try {
        result = await this.api.seatInventories(dialog.train.trainKey!, dialog.seatClass, this.draft.passengerCount);
      } catch (error) {
        // The request itself failed (429 included): leave the selection untouched and just report it.
        if (stale()) return;
        // 410 is the train key outliving the sheet; "다시 조회" alone read as "tap again", which cannot help.
        dialog.error = error instanceof ApiError && error.status === 410
          ? "열차 정보가 만료됐어요. 좌석표를 닫고 열차를 다시 조회한 뒤 골라 주세요."
          : error instanceof ApiError ? error.message : "모든 호차 좌석표를 불러오지 못했어요. 다시 시도해 주세요.";
        return;
      }
      if (stale()) return;
      const returnedCarNos = new Set(result.inventories.map((inventory) => inventory.carNo));
      // The cars at each end the user asked to leave out; the car on screen is never left out.
      const kept = keptCarNos(dialog);
      // A car missing from both the response and failedCars is still a car we couldn't apply to; count it as failed too.
      const failedCars = [...new Set([
        ...result.failedCars.filter((carNo) => kept.has(carNo)),
        ...dialog.cars.filter((car) => car.carNo !== currentCarNo && kept.has(car.carNo) && !returnedCarNos.has(car.carNo)).map((car) => car.carNo),
      ])].sort((a, b) => a - b);
      const nextSelected = dialog.selected.filter((seat) => seat.carNo === currentCarNo);
      for (const inventory of result.inventories) {
        dialog.inventories.set(inventory.carNo, inventory);
        if (inventory.carNo === currentCarNo) continue; // the car on screen keeps exactly its current selection
        if (!kept.has(inventory.carNo)) continue;
        const matched = filterEmpty
          ? inventory.seats.filter((seat) => currentLabels.has(seat.label))
          : filterSeats(inventory.seats, { ...filterSnapshot, trimRows: Math.min(filterSnapshot.trimRows, maxTrimRows(inventory.seats)) });
        nextSelected.push(...matched);
      }
      dialog.layoutReference ||= result.layoutReference;
      dialog.selected = nextSelected;
      dialog.appliedToAll = true;
      dialog.error = failedCars.length ? `${failedCars.join("·")}호차 좌석표를 불러오지 못해 빼고 적용했어요.` : "";
    } finally {
      if (!stale()) {
        dialog.bulkApplying = false;
        this.refreshSeatDialog(true);
      }
    }
  }

  // A seat is "available" (bookable right now) only when it's for sale and this isn't a reference layout from another day.
  private seatAvailable(dialog: SeatDialogState, seat: SeatMapSeat): boolean {
    return seat.salePossible && !dialog.layoutReference;
  }

  // Wait mode can pick any seat to wait for; immediate reservation can only take one that's actually available now.
  private seatSelectable(dialog: SeatDialogState, seat: SeatMapSeat): boolean {
    return dialog.mode === "wait" || this.seatAvailable(dialog, seat);
  }

  private toggleSeat(seatNo: string): void {
    const dialog = this.seatDialog;
    if (!dialog || dialog.bulkApplying) return;
    const seat = dialog.inventory?.seats.find((item) => item.seatNo === seatNo);
    if (!seat || !this.seatSelectable(dialog, seat)) return;
    const index = dialog.selected.findIndex((item) => item.carNo === seat.carNo && item.seatNo === seat.seatNo);
    if (index >= 0) dialog.selected.splice(index, 1);
    else {
      if (dialog.mode === "immediate") dialog.selected = dialog.selected.filter((item) => item.carNo === seat.carNo);
      if (dialog.mode === "immediate" && dialog.selected.length >= this.draft.passengerCount) dialog.selected.shift();
      dialog.selected.push(seat);
    }
    this.refreshSeatDialog(true);
  }

  private changeSeatFilter(action: string): void {
    const dialog = this.seatDialog;
    const inventory = dialog?.inventory;
    if (!dialog || !inventory || dialog.bulkApplying) return;
    const [kind, value = ""] = action.split(":");
    if (kind === "clear") {
      dialog.filter = emptySeatFilter();
      dialog.selected = [];
      dialog.appliedToAll = false;
      this.refreshSeatDialog(true);
      return;
    }
    const filter = dialog.filter;
    const toggle = (columns: string[], on: boolean) => {
      filter.columns = on
        ? [...new Set([...filter.columns, ...columns])]
        : filter.columns.filter((column) => !columns.includes(column));
    };
    if (kind === "col") toggle([value], !filter.columns.includes(value));
    if (kind === "pair") {
      const sets = seatColumnSets(inventory.seats);
      const columns = value === "aisle" ? sets.aisle : sets.window;
      toggle(columns, !columns.every((column) => filter.columns.includes(column)));
    }
    if (kind === "trim") {
      filter.trimRows = Math.max(0, Math.min(maxTrimRows(inventory.seats), filter.trimRows + (value === "+" ? 1 : -1)));
    }
    if (kind === "family") filter.excludeFamily = !filter.excludeFamily;
    if (kind === "cars") {
      dialog.trimCars = Math.max(0, Math.min(maxTrimCars(dialog.cars.length), dialog.trimCars + (value === "+" ? 1 : -1)));
    }
    // The conditions pick seats in the car on screen; seats already chosen in other cars stay chosen.
    // Turning the last condition off clears the car rather than selecting every seat in it.
    const empty = !filter.columns.length && !filter.trimRows && !filter.excludeFamily;
    const perCar = (seats: SeatMapSeat[]) => empty ? [] : filterSeats(seats, { ...filter, trimRows: Math.min(filter.trimRows, maxTrimRows(seats)) });
    if (dialog.mode === "immediate") {
      dialog.selected = perCar(inventory.seats).filter((seat) => seat.salePossible).slice(0, this.draft.passengerCount);
    } else if (dialog.appliedToAll) {
      // Every kept car follows the new condition, from the inventories the bulk apply cached; no request needed.
      const kept = keptCarNos(dialog);
      dialog.selected = [...dialog.inventories.values()].filter((cached) => kept.has(cached.carNo)).flatMap((cached) => perCar(cached.seats));
    } else {
      dialog.selected = [...dialog.selected.filter((seat) => seat.carNo !== inventory.carNo), ...perCar(inventory.seats)];
    }
    this.refreshSeatDialog(true);
  }

  private async confirmSeatDialog(): Promise<void> {
    const dialog = this.seatDialog;
    if (!dialog || dialog.bulkApplying) return;
    const passengerCount = this.draft.passengerCount;
    if (dialog.mode === "immediate") {
      if (dialog.selected.length !== passengerCount) {
        dialog.error = `${passengerCount}명의 좌석을 모두 선택해 주세요.`;
        this.refreshSeatDialog(true);
        return;
      }
      if (passengerCount > 1 && this.draft.seatStrategy === "1" && !consecutiveGroups(dialog.selected, passengerCount).length) {
        dialog.error = passengerCount >= 3
          ? "같은 줄에서 서로 붙어 있는 좌석을 선택해 주세요. 통로 건너편이어도 괜찮아요."
          : "서로 붙어 있는 연속 좌석을 선택해 주세요.";
        this.refreshSeatDialog(true);
        return;
      }
      await this.run(async (isCurrent) => {
        let result: DesignatedReservationResult;
        try {
          result = await this.api.reserveDesignated({
            trainKey: dialog.train.trainKey!, seatClass: dialog.seatClass,
            passengerCount, carNo: dialog.selected[0]!.carNo, seats: dialog.selected,
          });
        } catch (error) {
          if (error instanceof ApiError && error.status === 409 && this.seatDialog === dialog) {
            dialog.inventory = await this.api.seatInventory(
              dialog.train.trainKey!, dialog.carNo!, dialog.seatClass, passengerCount,
            );
            // Without this, tabbing away and back would hit loadSeatCar's cache and show the just-sold seat as selectable again.
            dialog.inventories.set(dialog.carNo!, dialog.inventory);
            dialog.selected = [];
            dialog.filter = emptySeatFilter();
            dialog.error = "선택한 좌석이 방금 판매됐어요. 새 좌석표에서 다시 선택해 주세요.";
            return;
          }
          throw error;
        }
        if (!isCurrent()) return;
        if (!result.reserved) {
          dialog.error = "선택한 좌석을 예약하지 못했어요. 좌석표를 다시 확인해 주세요.";
          return;
        }
        this.seatDialog = null;
        this.showToast("좌석을 예약했어요. 결제 기한을 확인해 주세요.");
        this.history = [];
        this.view = "activity";
        await this.reload();
      });
      return;
    }
    if (!dialog.selected.length) {
      dialog.error = "기다릴 좌석을 한 자리 이상 선택해 주세요.";
      this.refreshSeatDialog(true);
      return;
    }
    if (passengerCount > 1 && this.draft.seatStrategy === "1" && !consecutiveGroups(dialog.selected, passengerCount).length) {
      dialog.error = passengerCount >= 3
        ? "같은 줄에서 서로 붙어 있는 좌석 범위를 선택해 주세요. 통로 건너편이어도 괜찮아요."
        : `${passengerCount}명이 붙어 앉을 수 있는 연속 좌석 범위를 선택해 주세요.`;
      this.refreshSeatDialog(true);
      return;
    }
    // Picked seats replace this class's earlier picks and any whole-train pick of the same train.
    const next = this.cancellationTargets.filter((target) =>
      !(target.trainNo === dialog.train.no && (target.seatClass === dialog.seatClass || !target.targets.length)));
    // Only the fields the server reads; a full SeatMapSeat per seat bloats large multi-car plans.
    const targets: SeatTarget[] = dialog.selected.map(({ carNo, seatNo, label, row, column, direction, floor, adjacencyGroup, position, rowPosition }) => (
      { carNo, seatNo, label, row, column, direction, floor, adjacencyGroup, position, rowPosition }
    ));
    next.push({ trainNo: dialog.train.no, trainKey: dialog.train.trainKey, seatClass: dialog.seatClass, targets });
    this.cancellationTargets = next;
    this.seatDialog = null;
    this.error = "";
    this.showToast("취소표를 기다릴 좌석 범위를 저장했어요.");
    this.render();
  }

  // " (1편 좌석 지정 · 2편 좌석 무관)" after the train count, or nothing when every train is one kind.
  private describePlannedTrains(plan: CancellationWaitPlan | undefined): string {
    if (!plan) return "";
    const planned = new Set(plan.trains.filter((train) => train.targets.length).map((train) => train.trainNo)).size;
    const whole = new Set(plan.trains.filter((train) => !train.targets.length).map((train) => train.trainNo)).size;
    return planned && whole ? ` (${planned}편 좌석 지정 · ${whole}편 좌석 무관)` : planned ? " · 좌석 지정" : "";
  }

  // The class a train pressed as a whole is taken in: the one the conditions name, or either.
  private wholeTrainSeatClass(): SeatClass | "any" {
    const classes = this.draft.seatGradeMode === "specific" ? this.conditions?.seat_classes ?? [] : [];
    return classes.length === 1 ? classes[0]! : "any";
  }

  private toggleWholeTrain(trainNo: string): void {
    const rest = this.cancellationTargets.filter((target) => target.trainNo !== trainNo);
    // Pressing a card that already carries picked seats, or a whole-train pick, clears it; otherwise it takes the whole train.
    this.cancellationTargets = rest.length === this.cancellationTargets.length
      ? [...rest, { trainNo, seatClass: this.wholeTrainSeatClass(), targets: [] }]
      : rest;
    this.error = "";
    this.render();
  }

  // What the train list chose, folded into the conditions the confirm screen starts with.
  private applyTrainSelection(): void {
    // A draft restored from the server may predate v/action; buildConditions always fills them, this.conditions overrides the rest.
    const conditions = { ...buildConditions(this.draft), ...this.conditions };
    const selectedTrains = [...new Set(this.cancellationTargets.map((target) => target.trainNo))];
    // Only picked seats need a plan; trains taken whole are just the train list of a plain search.
    // With both kinds, every entry goes in the plan and the server seats the whole-train ones itself.
    const seatPlan: CancellationWaitPlan | undefined = this.cancellationTargets.some((target) => target.targets.length)
      ? {
          strategy: this.draft.passengerCount > 1 && this.draft.seatStrategy === "1" ? "consecutive" : "independent",
          passengerCount: this.draft.passengerCount,
          // The server never reads trainKey; drop it so the payload doesn't carry it.
          trains: this.cancellationTargets.map(({ trainNo, seatClass, targets }) => ({ trainNo, seatClass, targets })),
        }
      : undefined;
    this.conditions = { ...conditions, waitlist: false, trains: selectedTrains, seat_plan: seatPlan };
    this.selectedTrains = selectedTrains;
  }

  private renderConfirm(): string {
    const conditions = this.conditions ?? buildConditions(this.draft);
    const capabilities = this.state!.capabilities;
    const waitlist = conditions.waitlist;
    return `${this.renderSubhead("마지막 확인", waitlist ? "이 열차의 예약 대기를 신청할게요" : "이 조건으로 찾아드릴게요")}
      <section class="summary-card">
        <div class="route-hero"><span>${escapeHtml(conditions.src_station)}</span><i>→</i><span>${escapeHtml(conditions.dst_station)}</span></div>
        <dl>
          <dt>출발</dt><dd>${escapeHtml(formatWindow(conditions))}</dd>
          <dt>열차</dt><dd>${conditions.train_type === "1" ? "KTX 계열만" : "모든 열차"} · ${this.selectedTrains.length ? `${this.selectedTrains.length}편 선택${this.describePlannedTrains(conditions.seat_plan)}` : "시간대 전체"}</dd>
          <dt>좌석</dt><dd>${escapeHtml(SEAT_OPTIONS[conditions.seat_option])} · ${conditions.passenger_count}명</dd>
          ${conditions.passenger_count > 1 ? `<dt>배치</dt><dd>${conditions.seat_strategy === "1" ? "연속 좌석" : "랜덤 배치"}</dd>` : ""}
          <dt>좌석 지정</dt><dd>${escapeHtml(this.describeSeatPreference(conditions.seat_preference))}</dd>
          ${waitlist ? "<dt>신청 방식</dt><dd>코레일 예약 대기</dd>" : ""}
        </dl>
      </section>
      <section class="notice calm"><b>${waitlist ? "코레일 예약 대기만 신청해요." : "예약까지만 자동으로 진행해요."}</b><p>${waitlist ? "빈자리 자동 감시는 함께 돌리지 않아요. 배정 결과는 코레일 앱이나 홈페이지에서도 확인해 주세요." : "좌석이 예약되면 알려드려요. 결제는 안내된 기한 안에 코레일에서 직접 해 주세요."}</p></section>
      <label class="field favourite-name"><span>즐겨찾기 이름 <small>선택</small></span><input id="favourite-name" maxlength="40" placeholder="예: 주말에 집으로"></label>
      ${this.renderError()}
      ${this.renderAccessRequest()}
      <button class="button primary" data-action="start-now">${waitlist ? "코레일 예약 대기 신청" : "지금 빈자리 찾기"} <span>${waitlist ? "→" : "⌁"}</span></button>
      <button class="button ghost" data-action="schedule-toggle" ${capabilities.scheduledSearch && !waitlist ? "" : "disabled"}>검색 시작 시간 예약</button>
      ${waitlist ? '<p class="availability center">예약 대기는 선택한 열차에 바로 신청해요.</p>' : capabilities.scheduledSearch ? "" : '<p class="availability center">검색 예약은 현재 서버에서 지원하지 않아요.</p>'}
      ${this.scheduleOpen && capabilities.scheduledSearch && !waitlist ? `<section class="schedule-panel"><label class="field"><span>검색을 시작할 시각</span><input id="schedule-at" type="datetime-local" required></label><button class="button secondary" data-action="schedule-start">이 시각으로 검색 예약</button></section>` : ""}
      <div class="split-actions"><button class="text-button" data-action="save-favourite" ${capabilities.favourites ? "" : "disabled"}>☆ ${capabilities.favourites ? "이 조건 즐겨찾기" : "즐겨찾기 이용 불가"}</button><button class="text-button" data-view="journey">조건 수정</button></div>`;
  }

  private renderActivity(): string {
    const state = this.state!;
    const radar = deriveRadarView({
      running: this.radarRunning(state),
      connection: this.connection,
    });
    return `${this.renderSubhead("내 예약", "검색과 결제 상태")}
      ${this.renderPendingCard(false)}
      ${state.running ? `<section class="card activity-card"><div class="row-between"><span class="status-pill status-${radar.kind}"><i></i>${escapeHtml(radar.eyebrow)}</span><small>${radar.lastCheckedLabel ? `${escapeHtml(radar.lastCheckedLabel)} 확인` : "최근 조회 시각 없음"}</small></div><div class="route-hero small"><span>${escapeHtml(state.running.srcLocate)}</span><i>→</i><span>${escapeHtml(state.running.dstLocate)}</span></div><p class="center muted">${escapeHtml(formatWindow(state.running))} · ${state.running.passengerCount}명</p><div class="notice ${radar.kind === "error" || radar.kind === "stale" ? "warning" : "calm"}"><b>${escapeHtml(radar.title)}</b><p>${escapeHtml(radar.description)}</p></div><button class="button ghost danger" data-action="cancel-search">검색 중지</button></section>` : ""}
      ${state.scheduled ? this.renderScheduledCard() : ""}
      ${!state.running && !state.scheduled && !state.pending.length ? `<div class="empty">${activityEmptyMark()}<h2>진행 중인 검색이 없어요</h2><p>새 여정을 등록하고 빈자리를 찾아보세요.</p><button class="button primary" data-action="new-journey">새 여정 찾기</button></div>` : ""}
      <section class="timeline status-guide-card"><h2>상태 안내</h2><div><i></i><p><b>${escapeHtml(radar.title)}</b><span>${escapeHtml(radar.description)}</span></p></div>${state.running?.startedAt ? `<div><i></i><p><b>검색 시작</b><span>${escapeHtml(formatStamp(state.running.startedAt))}</span></p></div>` : ""}</section>`;
  }

  private renderFavourites(): string {
    const favourites = this.state!.favourites;
    if (!this.state!.capabilities.favourites) {
      return `${this.renderSubhead("즐겨찾기", "자주 가는 구간")}<div class="empty">${favouriteEmptyMark()}<h2>즐겨찾기를 이용할 수 없어요</h2><p>예약 서버에 연결하면 자주 쓰는 조건을 저장할 수 있어요.</p></div>`;
    }
    return `${this.renderSubhead("즐겨찾기", "자주 가는 구간")}
      <p class="intro">자주 가는 구간을 저장해 두고, 날짜만 바꿔 바로 조회하세요.</p>
      <div class="favourite-list">${favourites.length ? favourites.map((favourite) => this.renderFavouriteRow(favourite, false)).join("") : `<div class="empty">${favouriteEmptyMark()}<h2>즐겨찾기가 없어요</h2><p>자주 가는 구간을 저장해 두면 다음 검색이 더 빨라져요.</p></div>`}</div>
      <button class="button primary" data-action="new-journey">새 즐겨찾기 만들기 <span>＋</span></button>`;
  }

  private renderFavouriteRow(favourite: Favourite, compact: boolean): string {
    return `<article class="favourite-row ${compact ? "compact" : ""}"><button class="favourite-main" data-use-favourite="${escapeHtml(favourite.id)}"><span class="route-symbol">구간</span><span><b>${escapeHtml(favourite.name)}</b><small>${escapeHtml(favourite.route)} · ${escapeHtml(favourite.window)}</small></span></button>${compact ? "" : `<button class="icon-button danger" data-delete-favourite="${escapeHtml(favourite.id)}" aria-label="${escapeHtml(favourite.name)} 삭제">삭제</button>`}</article>`;
  }

  private renderNotifications(): string {
    const capable = this.state!.capabilities.durableNotifications;
    // "No notifications" is only true once the server has answered.
    const [title, text] = !capable
      ? ["알림 내역을 불러올 수 없어요", "현재 서버에서는 앱 알림 내역을 제공하지 않아요."]
      : this.notificationsLoaded
        ? ["새 알림이 없어요", "검색이나 예약 상태가 바뀌면 여기에 알려드려요."]
        : this.error
          ? ["알림을 불러오지 못했어요", "잠시 후 알림 화면을 다시 열어 주세요."]
          : ["알림을 불러오는 중이에요", "잠시만 기다려 주세요."];
    const items = this.notificationsLoaded && this.notifications.length
      ? this.notifications
          .map((item) => `<article class="notification"><span class="notification-icon">${item.kind === "reservation" ? "예약" : "검색"}</span><div><b>${escapeHtml(item.text)}</b><small>${escapeHtml(formatStamp(item.createdAt))}</small></div></article>`)
          .join("")
      : `<div class="empty"><span>◌</span><h2>${title}</h2><p>${text}</p></div>`;
    return `${this.renderSubhead("알림", "검색과 예약 소식")}${this.renderError()}${items}`;
  }

  private renderSettings(): string {
    const state = this.state!;
    const pushAvailable =
      state.capabilities.push &&
      state.notifications?.pushAvailable !== false &&
      state.pushAvailable !== false;
    const notifyAvailable = state.capabilities.notificationSettings;
    return `${this.renderSubhead("설정", "계정과 알림")}
      <div class="settings-layout"><aside class="settings-profile"><section class="profile-card"><span class="profile-avatar">${escapeHtml((state.user?.username || "나").slice(0, 1))}</span><div><b>${escapeHtml(state.user?.username || "여행자")}</b><small>${state.user?.role === "admin" ? "관리자 계정" : "초대로 가입한 계정"}</small></div></section><button class="button ghost danger" data-action="app-logout">앱에서 로그아웃</button></aside>
      <div class="settings-groups"><section class="settings-section"><p class="eyebrow">철도 계정</p><button class="settings-row" data-view="rail-account"><span><b>코레일 계정</b><small>${state.rail.registered ? "연결됨 · 모든 고속열차 예약 준비 완료" : "연결되지 않음"}</small></span><em>${state.rail.registered ? "관리" : "연결"} →</em></button><div class="settings-row static"><span><b>수서 출발 고속열차</b><small>별도 SRT 계정 없이 코레일 계정으로 이용해요</small></span><em class="integrated-badge">통합됨</em></div></section>
      <section class="settings-section"><p class="eyebrow">알림</p><div class="settings-row static"><span><b>검색 상황 알림</b><small>${notifyAvailable ? "검색 중 진행 상황을 알려드리는 간격" : "현재 서버에서는 알림 간격을 바꿀 수 없어요"}</small></span><div class="stepper small"><button data-action="notify-minus" ${notifyAvailable ? "" : "disabled"}>−</button><output>${notifyAvailable ? (state.notifyMinutes ? `${state.notifyMinutes}분` : "끔") : "이용 불가"}</output><button data-action="notify-plus" ${notifyAvailable ? "" : "disabled"}>＋</button></div></div><button class="settings-row" data-action="request-push" ${pushAvailable ? "" : "disabled"}><span><b>휴대폰 알림</b><small>${pushAvailable ? "Android 알림 권한 열기" : "휴대폰 알림 서비스가 아직 준비되지 않았어요"}</small></span><em>${pushAvailable ? "설정" : "이용 불가"}</em></button></section>
      ${state.user?.role === "admin" ? `<section class="settings-section admin-section"><div class="settings-section-title"><p class="eyebrow">회원 관리</p><span class="admin-only-badge">관리자 전용</span></div><p class="settings-section-copy">회원 가입 권한은 관리자만 발급할 수 있어요.</p><button class="settings-row" data-action="create-invite" ${this.inviteLoading ? "disabled" : ""}><span><b>회원 초대 코드</b><small>관리자만 만들 수 있는 일회용 가입 코드예요</small></span><em>${this.inviteLoading ? '<span class="inline-spinner" aria-hidden="true"></span><span class="sr-only">만드는 중</span>' : "만들기 →"}</em></button>${this.invitePreview ? `<div class="invite-card"><p>코드는 이 화면을 닫으면 다시 볼 수 없어요. 가입할 분에게 바로 전달해 주세요.</p><code>${escapeHtml(this.invitePreview)}</code><div class="invite-actions"><button class="button primary" data-action="copy-invite" type="button">복사</button><button class="button ghost" data-action="dismiss-invite" type="button">닫기</button></div></div>` : ""}</section>` : ""}
      <section class="settings-section"><p class="eyebrow">앱</p><button class="settings-row" data-action="theme"><span><b>화면 테마</b><small>시스템과 별도로 바꿀 수 있어요</small></span><em>${this.theme === "dark" ? "다크" : "라이트"}</em></button><div class="settings-row static"><span><b>앱 버전</b><small>서버 ${escapeHtml(state.version)}</small></span><em>v${escapeHtml(appPackage.version)}</em></div></section></div></div>`;
  }

  private renderRailAccount(): string {
    if (this.options.demoMode) {
      return `${this.renderSubhead("코레일 계정", "데모용 연결 상태")}<section class="notice calm"><b>샘플 계정으로 화면을 보여드려요.</b><p>데모에서는 코레일 아이디와 비밀번호를 받지 않고, 코레일 서버에도 연결하지 않아요.</p></section><button class="button ghost" data-action="back">설정으로 돌아가기</button>`;
    }
    const registered = this.state!.rail.registered;
    return `${this.renderSubhead("코레일 계정", registered ? "연결된 계정 관리" : "예약을 위한 계정 연결")}
      <section class="notice calm"><b>로그인 정보는 서버에서만 사용해요.</b><p>앱에는 코레일 비밀번호를 저장하지 않으며, 서버 응답에도 비밀번호를 담지 않아요.</p></section>
      ${registered ? `<section class="card"><div class="account-state"><span>✓</span><div><b>코레일 계정 연결됨</b><small>열차 조회와 예약을 시작할 수 있어요.</small></div></div><button class="button ghost danger" data-action="rail-logout">코레일 계정 연결 해제</button></section>` : `<form id="rail-form" class="form-stack"><label class="field"><span>휴대전화 번호 또는 회원번호</span><input name="username" inputmode="tel" maxlength="20" autocomplete="username" aria-describedby="rail-login-hint" required><small id="rail-login-hint">회원번호는 숫자 8자리 또는 10자리예요.</small></label><label class="field"><span>코레일 비밀번호</span><input name="password" type="password" maxlength="128" autocomplete="current-password" required></label>${this.state!.capabilities.korail ? "" : '<p class="availability center">예약 서버가 연결되지 않아 코레일 계정을 확인할 수 없어요.</p>'}${this.renderError()}<button class="button primary" type="submit" ${this.state!.capabilities.korail ? "" : "disabled"}>계정 확인하고 연결</button></form>`}`;
  }

  private renderAuth(): string {
    if (this.options.demoMode) {
      return `<div class="auth-shell with-demo"><aside class="demo-banner" data-demo-banner aria-label="데모 상태"><b>데모 모드</b><span>실제 조회·예약 없음</span></aside><div class="auth-art"><span class="orbit one"></span><span class="orbit two"></span><i>자</i></div><section class="auth-card"><p class="eyebrow">미리 둘러보기</p><h1>로그인 없이\n둘러보세요.</h1><p>데모에서는 샘플 데이터만 보여드려요. 로그인하거나 철도 서버에 요청을 보내지 않아요.</p><button class="button primary" data-action="demo-enter">샘플 화면 시작 <span>→</span></button></section></div>`;
    }
    const register = this.authMode === "register";
    const admin = this.authGate === "admin";
    const choosing = this.authGate === "choose";
    const title = choosing
      ? "어떻게\n로그인할까요?"
      : admin
        ? "관리자\n로그인"
        : register
          ? "초대 코드로\n시작해요."
          : "초대 회원\n로그인";
    const copy = choosing
      ? "관리자는 바로 로그인할 수 있고, 초대 회원은 받은 코드로 계정을 만들 수 있어요."
      : admin
        ? "관리자 아이디와 비밀번호를 입력하세요."
        : register
          ? "받은 코드와 사용할 아이디, 비밀번호를 입력하세요."
          : "가입할 때 만든 아이디와 비밀번호를 입력하세요.";
    return `<div class="auth-shell ${this.options.demoMode ? "with-demo" : ""}">
      ${this.options.demoMode ? '<aside class="demo-banner" data-demo-banner aria-label="데모 상태"><b>데모 모드</b><span>실제 조회·예약 없음</span></aside>' : ""}
      <div class="auth-art"><span class="orbit one"></span><span class="orbit two"></span><i>자</i></div>
      <section class="auth-card"><p class="eyebrow">내 여행의 빈자리</p><h1>${title}</h1><p>${copy}</p>
        ${choosing ? `<div class="gate-list"><button class="gate-card" data-auth-gate="admin" type="button"><b>관리자</b><small>관리자 계정으로 로그인해요</small></button><button class="gate-card" data-auth-gate="guest" type="button"><b>초대 회원</b><small>로그인하거나 초대 코드로 가입해요</small></button></div>` : ""}
        ${admin ? `<form id="auth-form" class="form-stack"><label class="field"><span>관리자 아이디</span><input name="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_]{3,32}" autocomplete="username" required></label><label class="field"><span>관리자 비밀번호</span><input name="password" type="password" minlength="12" maxlength="128" autocomplete="current-password" required></label>${this.renderError()}<button class="button primary" type="submit">관리자 로그인 <span>→</span></button></form><button class="text-button" data-auth-gate="choose" type="button">로그인 방법 바꾸기</button>` : ""}
        ${this.authGate === "guest" ? `<div class="segmented"><button data-auth-mode="login" class="${register ? "" : "active"}" type="button">로그인</button><button data-auth-mode="register" class="${register ? "active" : ""}" type="button">처음 가입</button></div>
        <form id="auth-form" class="form-stack">
          <label class="field"><span>앱 아이디</span><input name="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_]{3,32}" autocomplete="username" required></label>
          <label class="field"><span>앱 비밀번호</span><input name="password" type="password" minlength="12" maxlength="128" autocomplete="${register ? "new-password" : "current-password"}" required></label>
          ${register ? '<label class="field"><span>초대 코드</span><input name="invite" minlength="16" maxlength="128" autocomplete="one-time-code" required></label>' : ""}
          ${this.renderError()}
          <button class="button primary" type="submit">${register ? "가입하고 시작하기" : "로그인"} <span>→</span></button>
        </form>
        <button class="text-button" data-auth-gate="choose" type="button">로그인 방법 바꾸기</button>` : ""}
        ${this.options.demoMode ? '<button class="text-button demo-enter" data-action="demo-enter">샘플 화면 바로 보기</button>' : ""}
        <p class="auth-note">앱 계정과 코레일 계정은 서로 달라요. 결제는 코레일에서 직접 해 주세요.</p>
      </section>
      ${this.busy ? '<div class="blocker" role="status"><span class="spinner"></span><b>확인하고 있어요</b></div>' : ""}
    </div>`;
  }

  private renderUnavailable(): string {
    return `<div class="empty"><span>!</span><h2>앱을 열지 못했어요</h2><p>${escapeHtml(this.error || "서버 상태를 확인할 수 없어요.")}</p><button class="button primary" data-action="reload">다시 시도</button></div>`;
  }

  private renderNavigation(): string {
    const items: Array<[AppView, string]> = [
      ["home", "홈"],
      ["activity", "내 예약"],
      ["favourites", "즐겨찾기"],
      ["settings", "설정"],
    ];
    return `<nav class="bottom-nav" aria-label="주요 메뉴">${items.map(([view, label]) => `<button data-view="${view}" class="${this.view === view ? "active" : ""}" ${this.view === view ? 'aria-current="page"' : ""}><span>${label}</span></button>`).join("")}</nav>`;
  }

  private renderSubhead(eyebrow: string, title: string): string {
    return `<header class="subhead"><button class="back-button" data-action="back" type="button" aria-label="뒤로가기" title="뒤로가기"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 18-6-6 6-6"></path><path d="M9 12h10"></path></svg></button><div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h1>${escapeHtml(title)}</h1></div></header>`;
  }

  private renderAccessRequest(): string {
    if (!this.accessRequired) return "";
    return `<button class="button secondary" data-action="request-access" ${this.accessRequestPending ? "disabled" : ""}>${this.accessRequestPending ? "사용 승인 요청을 기다리는 중" : "운영자에게 사용 승인 요청"}</button>`;
  }

  private renderError(): string {
    return this.error ? `<p class="form-error" role="alert">${escapeHtml(this.error)}</p>` : "";
  }

  private radarRunning(state: BootstrapState): BootstrapState["running"] {
    if (!state.running || state.capabilities.lastChecked) return state.running;
    return { ...state.running, health: "unknown", lastCheckedAt: null };
  }

  private describeSeatPreference(encoded: string): string {
    if (!encoded) return "지정 없음";
    const [columns = "", rows = ""] = encoded.split(":");
    const [low = "", high = ""] = rows.split("-");
    const parts: string[] = [];
    if (columns) parts.push(`${columns.replaceAll(",", "·")}열`);
    if (low && high && low === high) parts.push(`${low}번`);
    else if (low && high) parts.push(`${low}–${high}번`);
    else if (low) parts.push(`${low}번 이상`);
    else if (high) parts.push(`${high}번 이하`);
    return parts.join(" ") || "지정 없음";
  }

  private readDraftFromForm(form: HTMLFormElement): BookingDraft {
    const data = new FormData(form);
    const value = (name: string): string => String(data.get(name) || "").trim();
    return {
      depDate: value("dep_date"),
      srcStation: value("src_station"),
      dstStation: value("dst_station"),
      depTime: value("dep_time"),
      maxDepTime: form.querySelector<HTMLInputElement>('[name="max_dep_time"]')?.value ?? this.draft.maxDepTime,
      unlimitedTime: data.has("unlimited_time"),
      trainType: value("train_type") === "2" ? "2" : "1",
      seatOption: this.draft.seatOption,
      passengerCount: this.draft.passengerCount,
      seatStrategy: data.has("seat_strategy")
        ? value("seat_strategy") === "2" ? "2" : "1"
        : this.draft.seatStrategy,
      seatColumns: data.getAll("seat_column").map(String),
      seatRowMin: value("seat_row_min"),
      seatRowMax: value("seat_row_max"),
      waitlist: data.has("waitlist"),
      seatGradeMode: value("seat_grade_mode") === "specific" ? "specific" : "any",
      seatClasses: data.getAll("seat_class").map(String).filter(
        (seatClass): seatClass is SeatClass => seatClass === "general" || seatClass === "special",
      ),
    };
  }

  private validateDraft(draft: BookingDraft): string | null {
    if (!draft.depDate || !draft.srcStation || !draft.dstStation || !draft.depTime) {
      return "구간과 날짜, 출발 시각을 모두 입력해 주세요.";
    }
    if (draft.srcStation === draft.dstStation) return "출발역과 도착역은 달라야 해요.";
    if (!draft.unlimitedTime && (!draft.maxDepTime || draft.maxDepTime <= draft.depTime)) {
      return "검색 종료 시각은 시작 시각보다 늦어야 해요.";
    }
    if (draft.seatGradeMode === "specific" && !(draft.seatClasses ?? []).length) {
      return "일반실이나 특실을 한 가지 이상 선택해 주세요.";
    }
    const low = draft.seatRowMin ? Number(draft.seatRowMin) : null;
    const high = draft.seatRowMax ? Number(draft.seatRowMax) : null;
    if ((low !== null && (low < 1 || low > 99)) || (high !== null && (high < 1 || high > 99))) {
      return "좌석 번호는 1부터 99 사이로 입력해 주세요.";
    }
    if (low !== null && high !== null && low > high) {
      return "첫 좌석 번호는 마지막 번호보다 클 수 없어요.";
    }
    return null;
  }

  private async findTrains(form: HTMLFormElement): Promise<void> {
    const draft = this.readDraftFromForm(form);
    const error = this.validateDraft(draft);
    if (error) {
      this.error = error;
      this.render();
      return;
    }
    this.draft = draft;
    this.conditions = buildConditions(draft);
    await this.run(async (isCurrent) => {
      const result = await this.api.trains({ conditions: this.conditions! });
      if (!isCurrent()) return;
      this.trainOptions = result.trains;
      this.trainListTruncated = result.truncated;
      this.selectedTrains = this.selectedTrains.filter((number) =>
        result.trains.some((train) => train.no === number),
      );
      // Drop seat-wait targets for trains a re-search (new route/date) no longer lists, or the start button would poll a train that's gone.
      this.cancellationTargets = this.cancellationTargets.filter((target) =>
        result.trains.some((train) => train.no === target.trainNo),
      );
      this.navigate("trains");
    });
  }

  private async startNow(): Promise<void> {
    // A draft restored from the server may predate v/action; buildConditions always fills them, this.conditions overrides the rest.
    const conditions = { ...buildConditions(this.draft), ...this.conditions };
    await this.run(async (isCurrent) => {
      const result = await this.api.search(buildBookingPayload(conditions, this.selectedTrains));
      if (!isCurrent()) return;
      if (result.needsAccessRequest) {
        this.accessRequired = true;
        this.accessRequestPending = result.accessRequestPending === true;
        this.error = result.accessRequestPending
          ? "이미 사용 승인을 요청했어요. 운영자의 답을 기다리고 있어요."
          : "체험 횟수를 모두 사용했어요. 아래에서 운영자에게 사용 승인을 요청할 수 있어요.";
        return;
      }
      if (!result.started) {
        if (result.waitlisted) {
          this.showToast(`${result.trainNo || "선택한 열차"}편 예약 대기를 신청했어요.`);
          this.history = [];
          this.view = "home";
          await this.reload();
          return;
        }
        this.error = "서버에서 검색 시작을 확인하지 못했어요.";
        return;
      }
      this.showToast("서버에서 검색을 시작했어요.");
      this.history = [];
      this.view = "home";
      await this.reload();
    });
  }

  private async schedule(): Promise<void> {
    const input = this.root.querySelector<HTMLInputElement>("#schedule-at");
    if (!input?.value) {
      this.error = "검색을 시작할 시각을 골라 주세요.";
      this.render();
      return;
    }
    const selected = new Date(input.value);
    if (Number.isNaN(selected.getTime())) {
      this.error = "검색 시작 시각을 확인할 수 없어요.";
      this.render();
      return;
    }
    const conditions = this.conditions ?? buildConditions(this.draft);
    await this.run(async (isCurrent) => {
      const result = await this.api.schedule({
        ...buildBookingPayload(conditions, this.selectedTrains),
        start_at: selected.toISOString(),
      });
      if (!isCurrent()) return;
      if (!result.scheduled) {
        this.error = "서버에서 시작 예약을 확인하지 못했어요.";
        return;
      }
      this.showToast(`${formatStamp(result.startAt)}에 검색을 시작해요.`);
      this.history = [];
      this.view = "home";
      await this.reload();
    });
  }

  private async saveFavourite(): Promise<void> {
    const conditions = this.conditions ?? buildConditions(this.draft);
    const name = this.root.querySelector<HTMLInputElement>("#favourite-name")?.value.trim();
    await this.run(async (isCurrent) => {
      const result = await this.api.saveFavourite({ conditions, ...(name ? { name } : {}) });
      if (!isCurrent()) return;
      if (!result.saved) {
        this.error = "즐겨찾기를 서버에 저장하지 못했어요.";
        return;
      }
      this.state!.favourites = result.favourites;
      this.showToast("즐겨찾기에 저장했어요.");
      this.render();
    });
  }

  private async loadNotifications(): Promise<void> {
    if (!this.state?.capabilities.durableNotifications) return;
    // Not run(): it drops the request while the app is busy, which would read as "no notifications".
    const generation = this.generation;
    try {
      const result = await this.api.notifications();
      if (generation !== this.generation) return;
      this.notifications = result.items;
      this.notificationsLoaded = true;
      this.state!.pushAvailable = result.pushAvailable;
    } catch (error) {
      if (generation !== this.generation) return;
      this.handleError(error);
      // handleError stays silent for a stale token; the screen must still leave "loading".
      if (generation === this.generation && !this.error) this.error = "알림을 불러오지 못했어요.";
    }
    if (generation === this.generation) this.render();
  }

  private async run(work: (isCurrent: () => boolean) => Promise<void>): Promise<void> {
    if (this.busy) return;
    const generation = this.generation;
    this.busy = true;
    this.error = "";
    this.render();
    try {
      await work(() => generation === this.generation);
    } catch (error) {
      if (generation === this.generation) this.handleError(error);
    } finally {
      if (generation === this.generation) {
        this.busy = false;
        this.render();
      }
    }
  }

  private handleError(error: unknown): void {
    if (error instanceof ApiError) {
      if (error.kind === "stale") return;
      if (error.kind === "auth") {
        this.resetSession();
        this.error = "로그인이 만료됐어요. 다시 로그인해 주세요.";
        this.render();
        return;
      }
      if (error.kind === "offline") this.connection = "offline";
      this.error = error.message;
      return;
    }
    this.error = error instanceof Error ? error.message : "문제가 생겼어요. 다시 시도해 주세요.";
  }

  private showToast(message: string): void {
    this.toast = message;
    if (this.toastTimer !== null) window.clearTimeout(this.toastTimer);
    this.toastTimer = window.setTimeout(() => {
      this.toast = "";
      this.render();
    }, 3_500);
  }

  private onClick(event: Event): void {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button");
    if (!button) return;
    if (button.dataset.trainToggle) {
      this.toggleWholeTrain(button.dataset.trainToggle);
      return;
    }
    if (button.dataset.seatMap !== undefined) {
      const seatClass = button.dataset.seatClass === "special" ? "special" : "general";
      const mode = button.dataset.seatMode === "wait" ? "wait" : "immediate";
      void this.openSeatMap(button.dataset.seatMap, button.dataset.trainNo || "", seatClass, mode);
      return;
    }
    if (button.dataset.immediateAny) {
      this.selectedTrains = [button.dataset.immediateAny];
      this.conditions = { ...(this.conditions ?? buildConditions(this.draft)), waitlist: false, seat_plan: undefined };
      void this.startNow();
      return;
    }
    if (button.dataset.officialWaitlist) {
      this.selectedTrains = [button.dataset.officialWaitlist];
      this.conditions = {
        ...(this.conditions ?? buildConditions(this.draft)), waitlist: true,
        seat_option: "2", seat_preference: "", seat_plan: undefined,
      };
      void this.startNow();
      return;
    }
    if (button.dataset.seatCar) {
      void this.loadSeatCar(Number(button.dataset.seatCar));
      return;
    }
    if (button.dataset.seatNo) {
      this.toggleSeat(button.dataset.seatNo);
      return;
    }
    if (button.dataset.seatFilter) {
      this.changeSeatFilter(button.dataset.seatFilter);
      return;
    }
    const view = button.dataset.view as AppView | undefined;
    if (view) {
      this.navigate(view);
      return;
    }
    const authGate = button.dataset.authGate;
    if (authGate === "choose" || authGate === "admin" || authGate === "guest") {
      this.authGate = authGate;
      this.authMode = "login";
      this.error = "";
      this.render();
      return;
    }
    const authMode = button.dataset.authMode;
    if (authMode === "login" || authMode === "register") {
      this.authMode = authMode;
      this.error = "";
      this.render();
      return;
    }
    const favouriteId = button.dataset.useFavourite;
    if (favouriteId) {
      const favourite = this.state?.favourites.find((item) => item.id === favouriteId);
      if (favourite) {
        this.draft = conditionsToDraft(favourite.conditions);
        this.conditions = null;
        this.selectedTrains = favourite.conditions.trains?.map(String) ?? [];
        this.navigate("journey");
      }
      return;
    }
    const deleteId = button.dataset.deleteFavourite;
    if (deleteId) {
      void this.run(async (isCurrent) => {
        const result = await this.api.deleteFavourite(deleteId);
        if (!isCurrent()) return;
        this.state!.favourites = result.favourites;
        this.showToast("즐겨찾기를 삭제했어요.");
      });
      return;
    }
    const action = button.dataset.action;
    switch (action) {
      case "back":
        this.back();
        break;
      case "reload":
        if (!this.busy) void this.reload();
        break;
      case "demo-enter":
        this.view = "home";
        void this.reload();
        break;
      case "new-journey":
        this.draft = conditionsToDraft(null);
        this.conditions = null;
        this.selectedTrains = [];
        this.trainOptions = [];
        this.trainListTruncated = false;
        this.seatDialog = null;
        this.cancellationTargets = [];
        this.navigate("journey");
        break;
      case "swap":
        this.syncJourneyDraft();
        [this.draft.srcStation, this.draft.dstStation] = [
          this.draft.dstStation,
          this.draft.srcStation,
        ];
        this.render(false);
        break;
      case "passenger-minus":
        this.syncJourneyDraft();
        this.draft.passengerCount = Math.max(1, this.draft.passengerCount - 1);
        this.render(false);
        break;
      case "passenger-plus":
        this.syncJourneyDraft();
        this.draft.passengerCount = Math.min(9, this.draft.passengerCount + 1);
        this.render(false);
        break;
      case "trains-next":
        this.applyTrainSelection();
        this.navigate("confirm");
        break;
      case "close-seat-dialog":
        this.seatDialog = null;
        this.error = "";
        this.render();
        break;
      case "confirm-seat-dialog":
        void this.confirmSeatDialog();
        break;
      case "retry-seat-dialog":
        void this.retrySeatDialog();
        break;
      case "apply-all-cars":
        void this.applyAllCars();
        break;
      case "start-now":
        void this.startNow();
        break;
      case "schedule-toggle":
        this.scheduleOpen = !this.scheduleOpen;
        this.render();
        break;
      case "schedule-start":
        void this.schedule();
        break;
      case "save-favourite":
        void this.saveFavourite();
        break;
      case "request-access":
        void this.run(async (isCurrent) => {
          const result = await this.api.requestAccess();
          if (!isCurrent()) return;
          this.accessRequestPending = result.requested && !result.approved;
          this.accessRequired = !result.approved;
          this.error = "";
          this.showToast(
            result.approved
              ? "이미 사용 승인을 받은 앱 계정이에요."
              : result.requested
                ? "운영자에게 사용 승인을 요청했어요."
                : "사용 승인 요청 상태를 확인해 주세요.",
          );
        });
        break;
      case "cancel-search":
        if (window.confirm("진행 중인 검색이나 예약된 검색을 취소할까요?")) {
          void this.run(async (isCurrent) => {
            const result = await this.api.cancelSearch();
            if (!isCurrent()) return;
            if (!result.stopped && !result.unscheduled) {
              this.error = "서버에서 중지할 검색을 찾지 못했어요.";
              return;
            }
            this.showToast("검색을 중지했어요.");
            await this.reload();
          });
        }
        break;
      case "cancel-pending":
        if (window.confirm("결제를 기다리는 예약을 모두 취소할까요?")) {
          void this.run(async (isCurrent) => {
            const result = await this.api.cancelReservations();
            if (!isCurrent()) return;
            if (!result.cancelled) {
              this.error = "서버에서 예약을 취소하지 못했어요.";
              return;
            }
            this.state!.pending = result.pending;
            this.showToast("예약을 취소했어요.");
          });
        }
        break;
      case "notify-minus":
        void this.changeNotify(-1);
        break;
      case "notify-plus":
        void this.changeNotify(1);
        break;
      case "theme":
        this.theme = this.theme === "dark" ? "light" : "dark";
        window.localStorage.setItem("jari.theme", this.theme);
        this.render();
        break;
      case "request-push":
        void this.options.onRequestPush?.();
        break;
      case "create-invite":
        void this.createInvite();
        break;
      case "copy-invite":
        if (this.invitePreview) {
          void navigator.clipboard?.writeText(this.invitePreview).then(
            () => this.showToast("초대 코드를 복사했어요."),
            () => this.showToast("이 기기에서는 복사할 수 없어요. 코드를 직접 전달해 주세요."),
          );
        }
        break;
      case "dismiss-invite":
        this.invitePreview = "";
        this.render();
        break;
      case "rail-logout":
        if (window.confirm("코레일 계정 연결을 해제할까요? 진행 중인 검색은 따로 중지해야 해요.")) {
          void this.run(async (isCurrent) => {
            const result = await this.api.railwayLogout();
            if (!isCurrent()) return;
            this.state!.rail.registered = result.registered;
            this.showToast("코레일 계정 연결을 해제했어요.");
            this.render();
          });
        }
        break;
      case "app-logout": {
        // Start revocation with A's token, then invalidate all A UI work immediately.
        const revocation = this.api.logoutApp().catch(() => undefined);
        const clearing = this.options.onLogout?.();
        this.resetSession();
        this.render();
        void Promise.all([revocation, clearing]).catch(() => undefined);
      }
        break;
    }
  }

  private onChange(event: Event): void {
    const target = event.target as HTMLInputElement;
    if (target.closest("#conditions-form")) {
      this.syncJourneyDraft();
      if (target.name === "unlimited_time" || target.name === "seat_grade_mode") this.render();
    }
  }

  private syncJourneyDraft(): void {
    const form = this.root.querySelector<HTMLFormElement>("#conditions-form");
    if (form) this.draft = this.readDraftFromForm(form);
  }

  private onSubmit(event: Event): void {
    event.preventDefault();
    const form = event.target as HTMLFormElement;
    if (form.id === "conditions-form") {
      void this.findTrains(form);
    } else if (form.id === "auth-form") {
      void this.submitAuth(form);
    } else if (form.id === "rail-form") {
      void this.submitRail(form);
    }
  }

  private async submitAuth(form: HTMLFormElement): Promise<void> {
    const data = new FormData(form);
    const username = String(data.get("username") || "").trim();
    const password = String(data.get("password") || "");
    const invite = String(data.get("invite") || "").trim();
    await this.run(async (isCurrent) => {
      const result =
        this.authMode === "register" && this.authGate === "guest"
          ? await this.api.registerApp({ username, password, invite })
          : await this.api.login({
              username,
              password,
              role: this.authGate === "admin" ? "admin" : "member",
            });
      if (!isCurrent()) return;
      if (!result.token) {
        this.error = "서버에서 로그인을 완료하지 못했어요.";
        return;
      }
      const generation = this.generation;
      await this.options.onToken?.(result.token);
      if (generation !== this.generation) return;
      await this.start(true);
    });
  }

  private async submitRail(form: HTMLFormElement): Promise<void> {
    const data = new FormData(form);
    const username = String(data.get("username") || "").trim();
    const password = String(data.get("password") || "");
    await this.run(async (isCurrent) => {
      const result = await this.api.railwayRegister({ username, password });
      if (!isCurrent()) return;
      if (!result.registered) {
        this.error = "서버에서 코레일 계정 연결을 완료하지 못했어요.";
        return;
      }
      this.state!.rail.registered = true;
      this.showToast("코레일 계정을 연결했어요.");
      this.view = "settings";
    });
  }

  private async changeNotify(direction: number): Promise<void> {
    const state = this.state!;
    const previous = state.notifyMinutes;
    // A value the server kept from elsewhere sits between steps: move to the neighbouring step.
    const above = NOTIFY_STEPS.findIndex((step) => step >= previous);
    const at = above < 0 ? NOTIFY_STEPS.length : above;
    const current = NOTIFY_STEPS[at] === previous || direction < 0 ? at : at - 1;
    const next = Math.min(NOTIFY_STEPS.length - 1, Math.max(0, current + direction));
    const minutes = NOTIFY_STEPS[next]!;
    if (minutes === previous) return;
    const version = ++this.notifySaveVersion;
    state.notifyMinutes = minutes;
    this.render();
    try {
      const result = await this.api.setNotify(minutes);
      if (version !== this.notifySaveVersion || this.state !== state) return;
      state.notifyMinutes = result.notifyMinutes;
      this.showToast(result.notifyMinutes ? `${result.notifyMinutes}분마다 알려드려요.` : "검색 상황 알림을 껐어요.");
      this.render();
    } catch {
      if (version !== this.notifySaveVersion || this.state !== state) return;
      state.notifyMinutes = previous;
      this.showToast("알림 간격을 저장하지 못했어요. 잠시 후 다시 시도해 주세요.");
      this.render();
    }
  }

  private async createInvite(): Promise<void> {
    if (this.inviteLoading) return;
    const generation = this.generation;
    this.inviteLoading = true;
    this.render();
    try {
      const result = await this.api.createInvite({ ttlHours: 72 });
      if (generation !== this.generation) return;
      this.invitePreview = result.invite;
      this.showToast("초대 코드를 만들었어요. 이 화면에서만 확인할 수 있어요.");
    } catch (error) {
      if (generation !== this.generation) return;
      this.handleError(error);
    } finally {
      if (generation === this.generation) {
        this.inviteLoading = false;
        this.render();
      }
    }
  }
}
