import { ApiError } from "./api";
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
import type {
  BootstrapState,
  Conditions,
  Favourite,
  MobileApi,
  NotificationItem,
  SearchDescription,
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
}

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

export class TeumApp {
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
  private notifications: NotificationItem[] = [];
  private authGate: "choose" | "admin" | "guest" = "choose";
  private authMode: "login" | "register" = "login";
  private invitePreview = "";
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
    const savedTheme = window.localStorage.getItem("teum.theme");
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
    this.notifications = [];
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
    if (next === "notifications") void this.loadNotifications();
  }

  back(): boolean {
    this.syncJourneyDraft();
    const previous = this.history.pop();
    if (!previous) return false;
    this.view = previous;
    this.error = "";
    this.render();
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
      } else {
        this.connection = "offline";
      }
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
        ${this.connection === "offline" ? '<aside class="offline-banner" aria-label="연결 상태">오프라인 · 마지막으로 받은 상태를 표시합니다</aside>' : ""}
        <header class="topbar">
          <button class="brand" data-view="home" aria-label="틈 홈"><span class="brand-mark">틈</span><span><b>틈</b><small>내 여행의 빈자리</small></span></button>
          <button class="icon-button" data-view="notifications" aria-label="알림"><span aria-hidden="true">◌</span></button>
        </header>
        <main class="screen">${content}</main>
        ${this.renderNavigation()}
        ${this.busy ? '<div class="blocker" role="status"><span class="spinner"></span><b>잠시만 기다려주세요</b></div>' : ""}
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
        ? { kind: "reserved", title: "빈자리를 찾았어요", description: "아래 예약 내역에서 결제 기한을 확인해주세요." }
        : !state.running && state.scheduled
          ? { kind: "scheduled", title: "예약한 시각에 검색을 시작해요", description: `${formatStamp(state.scheduled.startAt)} 시작 예정` }
          : radar
      : radar;
    return `
      <section class="status-heading">
        <div><p class="eyebrow">MY SEARCH STATUS</p><h1>나의 검색 현황</h1></div>
      </section>
      <section class="card search-status-card" data-search-status data-state="${status.kind}" aria-label="검색 상태 요약">
        <div class="search-status-title"><span class="search-status-icon" aria-hidden="true">${status.kind === "reserved" ? "✓" : ["error", "stale", "offline"].includes(status.kind) ? "!" : "•"}</span><h2>${escapeHtml(status.title)}</h2></div>
        ${route}
        ${journey ? `<p class="search-conditions">${escapeHtml(journey.trainTypeShow)} · ${escapeHtml(seatLabels[journey.specialInfoShow] ?? journey.specialInfoShow)} · ${journey.passengerCount}명</p>` : ""}
        <p class="search-status-description">${escapeHtml(status.description)}</p>
        ${!paymentFirst && state.running && radar.lastCheckedLabel ? `<p class="search-last-check">마지막 조회 ${escapeHtml(radar.lastCheckedLabel)}</p>` : ""}
        ${state.running || state.scheduled || state.pending.length ? `<button class="button secondary" data-view="activity">${paymentFirst ? "예약 확인하기" : "검색 상세 보기"}<span>→</span></button>` : ""}
      </section>
      ${this.renderPendingCard(true)}
      ${state.scheduled ? this.renderScheduledCard() : ""}
      <section class="section-head"><div><p class="eyebrow">RECENT ROUTES</p><h2>자주 가는 구간</h2></div><button class="text-button" data-view="favourites">전체 보기</button></section>
      <div class="route-list">${state.favourites.length ? state.favourites.slice(0, 2).map((favourite) => this.renderFavouriteRow(favourite, true)).join("") : '<div class="empty compact"><p>저장한 구간이 아직 없어요.</p></div>'}</div>
      <button class="button primary roomy" data-view="journey" ${state.capabilities.korail ? "" : "disabled"}><span>${state.capabilities.korail ? "새 여정 찾기" : "예약 서버 연결 필요"}</span><b>＋</b></button>
    `;
  }

  private renderPendingCard(compact: boolean): string {
    const pending = this.state!.pending;
    if (!pending.length) return "";
    return `<section class="card payment-card ${compact ? "compact-card" : ""}">
      <div class="card-label warning"><i></i>결제가 필요한 예약 ${pending.length}건</div>
      ${pending.map((item) => `<div class="payment-row"><div><strong>${escapeHtml(item.trainInfo)}</strong><small>${item.seatNumber !== null ? `좌석 ${escapeHtml(item.seatNumber)} · ` : ""}${item.reservationId ? `예약번호 ${escapeHtml(item.reservationId)}` : "예약번호 정보 없음"}</small></div><b>${item.expiresAt ? `${escapeHtml(formatStamp(item.expiresAt))}까지` : "결제 기한 미제공"}</b></div>`).join("")}
      <a class="button primary" href="${escapeHtml(this.state!.paymentUrl)}" target="_blank" rel="noreferrer">코레일에서 결제하기 <span>↗</span></a>
      ${compact ? '<button class="text-button danger" data-view="activity">예약 관리</button>' : '<button class="button ghost danger" data-action="cancel-pending">예약 전체 취소</button>'}
    </section>`;
  }

  private renderScheduledCard(): string {
    const scheduled = this.state!.scheduled!;
    return `<section class="card scheduled-card"><div class="card-label"><i></i>검색 시작 예약</div><div class="route-line"><strong>${escapeHtml(scheduled.search.srcLocate)} → ${escapeHtml(scheduled.search.dstLocate)}</strong><span>${escapeHtml(formatStamp(scheduled.startAt))} 시작</span></div><button class="text-button danger" data-action="cancel-search">시작 예약 취소</button></section>`;
  }

  private renderJourney(): string {
    const state = this.state!;
    const capabilities = state.capabilities;
    const stationOptions = state.rail.stations
      .map((station) => `<option value="${escapeHtml(station)}"></option>`)
      .join("");
    const column = (letter: string, position: string) => `
      <label class="seat-column"><input type="checkbox" name="seat_column" value="${letter}" ${this.draft.seatColumns.includes(letter) ? "checked" : ""}><span><b>${letter}</b><small>${position}</small></span></label>`;
    return `${this.renderSubhead("새 여정", "여행 조건을 알려주세요")}
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
            <button class="choice-disabled" type="button" data-operator="srt" disabled aria-describedby="srt-note"><b>SRT</b><small>현재 이용 불가</small></button>
          </fieldset>
          <p id="srt-note" class="availability">${capabilities.srt ? "SRT 계약은 아직 이 클라이언트 제출 형식에 연결되지 않았어요." : "SRT는 현재 서버에서 지원하지 않아요."}</p>
        </section>
        <section class="card form-card">
          <div class="form-section-head"><div><span>02</span><h2>언제 떠나세요?</h2></div></div>
          <label class="field"><span>출발 날짜</span><input name="dep_date" type="date" value="${escapeHtml(this.draft.depDate)}" required></label>
          <div class="two-columns">
            <label class="field"><span>이 시간부터</span><input name="dep_time" type="time" value="${escapeHtml(this.draft.depTime)}" required></label>
            <label class="field"><span>이 시간까지</span><input name="max_dep_time" type="time" value="${escapeHtml(this.draft.maxDepTime)}" ${this.draft.unlimitedTime ? "disabled" : "required"}></label>
          </div>
          <label class="check-row"><input name="unlimited_time" type="checkbox" ${this.draft.unlimitedTime ? "checked" : ""}><span><b>마지막 열차까지 찾기</b><small>종료 시각을 24:00으로 설정합니다</small></span></label>
        </section>
        <section class="card form-card">
          <div class="form-section-head"><div><span>03</span><h2>어떤 좌석이 좋으세요?</h2></div></div>
          <fieldset class="choice-grid"><legend>좌석 등급</legend>
            ${this.radioCard("seat_option", "1", "일반실 우선", "없으면 특실", this.draft.seatOption === "1")}
            ${this.radioCard("seat_option", "2", "일반실만", "일반실 한정", this.draft.seatOption === "2")}
            ${this.radioCard("seat_option", "3", "특실 우선", "없으면 일반실", this.draft.seatOption === "3")}
            ${this.radioCard("seat_option", "4", "특실만", "특실 한정", this.draft.seatOption === "4")}
          </fieldset>
          <div class="passenger-row"><span><b>인원</b><small>최대 9명</small></span><div class="stepper"><button type="button" data-action="passenger-minus">−</button><output>${this.draft.passengerCount}명</output><button type="button" data-action="passenger-plus">＋</button></div></div>
          ${this.draft.passengerCount > 1 ? `<fieldset class="choice-grid"><legend>좌석 배치</legend>${this.radioCard("seat_strategy", "1", "연속 좌석", "붙어 있는 자리만", this.draft.seatStrategy === "1")}${this.radioCard("seat_strategy", "2", "랜덤 배치", "떨어져도 예약", this.draft.seatStrategy === "2")}</fieldset>` : ""}
          <details class="advanced"><summary>세부 좌석 조건 <span>열·행 지정</span></summary>
            <fieldset><legend>좌석 열</legend><div class="seat-map">${column("A", "창측")}${column("B", "내측")}<span class="aisle">통로</span>${column("C", "내측")}${column("D", "창측")}</div></fieldset>
            <div class="row-range"><label class="field"><span>첫 좌석 번호</span><input name="seat_row_min" type="number" min="1" max="99" inputmode="numeric" value="${escapeHtml(this.draft.seatRowMin)}" placeholder="예: 1"></label><span>–</span><label class="field"><span>마지막 좌석 번호</span><input name="seat_row_max" type="number" min="1" max="99" inputmode="numeric" value="${escapeHtml(this.draft.seatRowMax)}" placeholder="예: 15"></label></div>
            <p class="field-note">비워두면 아무 좌석이나 잡습니다. 조건이 좁을수록 검색 성공까지 더 오래 걸릴 수 있어요.</p>
          </details>
          <label class="check-row unavailable-control"><input name="waitlist" type="checkbox" disabled><span><b>예약대기도 함께 신청</b><small>${capabilities.waitlist ? "제출 형식 연결 전이라 사용할 수 없어요" : "현재 서버에서 지원하지 않아요"}</small></span></label>
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
            const selected = this.selectedTrains.includes(train.no);
            const departure = train.dep_time ? clockFromCompact(train.dep_time) : "";
            const arrival = train.arr_time ? clockFromCompact(train.arr_time) : "";
            return `<button class="train-card ${selected ? "selected" : ""}" data-train="${escapeHtml(train.no)}" type="button" aria-pressed="${selected}"><span class="train-check">${selected ? "✓" : ""}</span><span class="train-main"><small>${escapeHtml(train.name || `열차 ${train.no}`)}</small><b>${departure && arrival ? `${escapeHtml(departure)} <i>→</i> ${escapeHtml(arrival)}` : escapeHtml(train.label)}</b><em>${escapeHtml(train.label)}</em></span><span class="seat-badge ${train.soldout ? "soldout" : "available"}">${train.soldout ? "매진" : "여석 있음"}</span></button>`;
          })
          .join("")
      : '<div class="empty"><span>⌁</span><h2>조회된 열차가 없어요</h2><p>선택 없이 시간대 전체를 계속 감시할 수 있습니다.</p></div>';
    return `${this.renderSubhead("열차 선택", "어떤 열차를 기다릴까요?")}
      <div class="context-line"><b>${escapeHtml(conditions.src_station)} → ${escapeHtml(conditions.dst_station)}</b><span>${escapeHtml(formatWindow(conditions))}</span></div>
      <p class="intro">여러 편을 선택하면 그중 먼저 빈자리가 생긴 열차를 예약합니다. 선택하지 않으면 시간대 전체를 감시해요.</p>
      ${this.trainListTruncated ? '<div class="notice warning">목록이 길어 일부 열차만 표시합니다. 시간대 전체 감시는 그대로 이용할 수 있어요.</div>' : ""}
      <div class="train-list">${list}</div>
      ${this.renderError()}
      <button class="button primary sticky-action" data-action="trains-next">${this.selectedTrains.length ? `${this.selectedTrains.length}편 선택 · 조건 확인` : "시간대 전체 감시"} <span>→</span></button>`;
  }

  private renderConfirm(): string {
    const conditions = this.conditions ?? buildConditions(this.draft);
    const capabilities = this.state!.capabilities;
    return `${this.renderSubhead("최종 확인", "이 조건으로 찾아드릴게요")}
      <section class="summary-card">
        <div class="route-hero"><span>${escapeHtml(conditions.src_station)}</span><i>→</i><span>${escapeHtml(conditions.dst_station)}</span></div>
        <dl>
          <dt>출발</dt><dd>${escapeHtml(formatWindow(conditions))}</dd>
          <dt>열차</dt><dd>${conditions.train_type === "1" ? "KTX 계열만" : "모든 열차"} · ${this.selectedTrains.length ? `${this.selectedTrains.length}편 선택` : "시간대 전체"}</dd>
          <dt>좌석</dt><dd>${escapeHtml(SEAT_OPTIONS[conditions.seat_option])} · ${conditions.passenger_count}명</dd>
          ${conditions.passenger_count > 1 ? `<dt>배치</dt><dd>${conditions.seat_strategy === "1" ? "연속 좌석" : "랜덤 배치"}</dd>` : ""}
          <dt>좌석 지정</dt><dd>${escapeHtml(this.describeSeatPreference(conditions.seat_preference))}</dd>
        </dl>
      </section>
      <section class="notice calm"><b>예약까지만 자동으로 진행해요.</b><p>좌석을 확보하면 알려드립니다. 결제는 안내된 기한 안에 코레일에서 직접 해주세요.</p></section>
      <label class="field favourite-name"><span>즐겨찾기 이름 <small>선택</small></span><input id="favourite-name" maxlength="40" placeholder="예: 주말에 집으로"></label>
      ${this.renderError()}
      ${this.accessRequired ? `<button class="button secondary" data-action="request-access" ${this.accessRequestPending ? "disabled" : ""}>${this.accessRequestPending ? "사용 승인 요청을 기다리는 중" : "운영자에게 사용 승인 요청"}</button>` : ""}
      <button class="button primary" data-action="start-now">지금 빈자리 찾기 <span>⌁</span></button>
      <button class="button ghost" data-action="schedule-toggle" ${capabilities.scheduledSearch ? "" : "disabled"}>원하는 시각에 검색 시작</button>
      ${capabilities.scheduledSearch ? "" : '<p class="availability center">시작 예약은 현재 서버에서 지원하지 않아요.</p>'}
      ${this.scheduleOpen && capabilities.scheduledSearch ? `<section class="schedule-panel"><label class="field"><span>검색을 시작할 시각</span><input id="schedule-at" type="datetime-local" required></label><button class="button secondary" data-action="schedule-start">이 시각에 시작 예약</button></section>` : ""}
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
      ${state.running ? `<section class="card activity-card"><div class="row-between"><span class="status-pill status-${radar.kind}"><i></i>${escapeHtml(radar.eyebrow)}</span><small>${radar.lastCheckedLabel ? `${escapeHtml(radar.lastCheckedLabel)} 확인` : "최근 조회 시각 미제공"}</small></div><div class="route-hero small"><span>${escapeHtml(state.running.srcLocate)}</span><i>→</i><span>${escapeHtml(state.running.dstLocate)}</span></div><p class="center muted">${escapeHtml(formatWindow(state.running))} · ${state.running.passengerCount}명</p><div class="notice ${radar.kind === "error" || radar.kind === "stale" ? "warning" : "calm"}"><b>${escapeHtml(radar.title)}</b><p>${escapeHtml(radar.description)}</p></div><button class="button ghost danger" data-action="cancel-search">검색 중지</button></section>` : ""}
      ${state.scheduled ? this.renderScheduledCard() : ""}
      ${!state.running && !state.scheduled && !state.pending.length ? '<div class="empty"><span>⌁</span><h2>진행 중인 여정이 없어요</h2><p>다음 여행의 빈자리를 찾아보세요.</p><button class="button primary" data-view="journey">새 여정 찾기</button></div>' : ""}
      <section class="timeline"><h2>상태 안내</h2><div><i></i><p><b>${escapeHtml(radar.title)}</b><span>${escapeHtml(radar.description)}</span></p></div>${state.running?.startedAt ? `<div><i></i><p><b>검색 시작</b><span>${escapeHtml(formatStamp(state.running.startedAt))}</span></p></div>` : ""}</section>`;
  }

  private renderFavourites(): string {
    const favourites = this.state!.favourites;
    if (!this.state!.capabilities.favourites) {
      return `${this.renderSubhead("즐겨찾기", "자주 가는 구간")}<div class="empty"><span>☆</span><h2>즐겨찾기 기능을 지원하지 않아요</h2><p>예약 서버가 연결되면 자주 쓰는 조건을 저장할 수 있습니다.</p></div>`;
    }
    return `${this.renderSubhead("즐겨찾기", "자주 가는 구간")}
      <p class="intro">저장한 조건에 새 날짜만 골라 빠르게 열차를 조회할 수 있어요.</p>
      <div class="favourite-list">${favourites.length ? favourites.map((favourite) => this.renderFavouriteRow(favourite, false)).join("") : '<div class="empty"><span>☆</span><h2>저장한 구간이 없어요</h2><p>최종 확인 화면에서 현재 조건을 저장할 수 있습니다.</p></div>'}</div>
      <button class="button primary" data-view="journey">새 조건 만들기 <span>＋</span></button>`;
  }

  private renderFavouriteRow(favourite: Favourite, compact: boolean): string {
    return `<article class="favourite-row ${compact ? "compact" : ""}"><button class="favourite-main" data-use-favourite="${escapeHtml(favourite.id)}"><span class="route-symbol">↗</span><span><b>${escapeHtml(favourite.name)}</b><small>${escapeHtml(favourite.route)} · ${escapeHtml(favourite.window)}</small></span></button>${compact ? "" : `<button class="icon-button danger" data-delete-favourite="${escapeHtml(favourite.id)}" aria-label="${escapeHtml(favourite.name)} 삭제">×</button>`}</article>`;
  }

  private renderNotifications(): string {
    const capable = this.state!.capabilities.durableNotifications;
    const items = this.notifications.length
      ? this.notifications
          .map((item) => `<article class="notification"><span class="notification-icon">${item.kind === "reservation" ? "✓" : "⌁"}</span><div><b>${escapeHtml(item.text)}</b><small>${escapeHtml(formatStamp(item.createdAt))}</small></div></article>`)
          .join("")
      : `<div class="empty"><span>◌</span><h2>${capable ? "새 알림이 없어요" : "알림 기록을 불러올 수 없어요"}</h2><p>${capable ? "검색과 예약 상태가 바뀌면 여기에 기록됩니다." : "현재 서버에서 앱 알림 기록을 지원하지 않습니다."}</p></div>`;
    return `${this.renderSubhead("알림", "여정의 중요한 순간")}${items}`;
  }

  private renderSettings(): string {
    const state = this.state!;
    const pushAvailable =
      state.capabilities.push &&
      state.notifications?.pushAvailable !== false &&
      state.pushAvailable !== false;
    const notifyAvailable = state.capabilities.notificationSettings;
    return `${this.renderSubhead("설정", "계정과 알림")}
      <section class="profile-card"><span class="profile-avatar">${escapeHtml((state.user?.username || "나").slice(0, 1))}</span><div><b>${escapeHtml(state.user?.username || "여행자")}</b><small>초대로 함께하는 틈</small></div></section>
      <section class="settings-section"><p class="eyebrow">RAILWAY ACCOUNT</p><button class="settings-row" data-view="rail-account"><span><b>코레일 계정</b><small>${state.rail.registered ? "연결됨 · 예약 준비 완료" : "연결되지 않음"}</small></span><em>${state.rail.registered ? "관리" : "연결"} →</em></button><div class="settings-row disabled"><span><b>SRT 계정</b><small>현재 서버에서 지원하지 않아요</small></span><em>이용 불가</em></div></section>
      <section class="settings-section"><p class="eyebrow">NOTIFICATIONS</p><div class="settings-row static"><span><b>검색 진행 알림</b><small>${notifyAvailable ? "서버가 검색 경과를 알려주는 간격" : "현재 서버에서 설정을 지원하지 않아요"}</small></span><div class="stepper small"><button data-action="notify-minus" ${notifyAvailable ? "" : "disabled"}>−</button><output>${notifyAvailable ? (state.notifyMinutes ? `${state.notifyMinutes}분` : "끔") : "이용 불가"}</output><button data-action="notify-plus" ${notifyAvailable ? "" : "disabled"}>＋</button></div></div><button class="settings-row" data-action="request-push" ${pushAvailable ? "" : "disabled"}><span><b>기기 푸시 알림</b><small>${pushAvailable ? "Android 알림 권한 설정" : "Firebase가 구성되지 않아 사용할 수 없어요"}</small></span><em>${pushAvailable ? "설정" : "이용 불가"}</em></button></section>
      ${state.user?.role === "admin" ? `<section class="settings-section"><p class="eyebrow">INVITES</p><button class="settings-row" data-action="create-invite"><span><b>선택받은 자 초대 코드</b><small>한 번만 쓸 수 있는 코드를 만듭니다</small></span><em>만들기 →</em></button>${this.invitePreview ? `<div class="invite-card"><p>이 코드는 지금만 다시 보여줍니다. 선택받은 자에게 전해주세요.</p><code>${escapeHtml(this.invitePreview)}</code><div class="invite-actions"><button class="button primary" data-action="copy-invite" type="button">복사</button><button class="button ghost" data-action="dismiss-invite" type="button">닫기</button></div></div>` : ""}</section>` : ""}
      <section class="settings-section"><p class="eyebrow">APP</p><button class="settings-row" data-action="theme"><span><b>화면 테마</b><small>시스템과 별도로 바꿀 수 있어요</small></span><em>${this.theme === "dark" ? "다크" : "라이트"}</em></button><div class="settings-row static"><span><b>앱 버전</b><small>서버 ${escapeHtml(state.version)}</small></span><em>Beta</em></div></section>
      <button class="button ghost danger" data-action="app-logout">앱에서 로그아웃</button>`;
  }

  private renderRailAccount(): string {
    if (this.options.demoMode) {
      return `${this.renderSubhead("코레일 계정", "데모용 연결 상태")}<section class="notice calm"><b>샘플 계정으로만 화면을 보여드려요.</b><p>데모 모드에서는 실제 코레일 자격증명을 입력받지 않아요. 로그인이나 철도 요청도 보내지 않습니다.</p></section><button class="button ghost" data-action="back">설정으로 돌아가기</button>`;
    }
    const registered = this.state!.rail.registered;
    return `${this.renderSubhead("코레일 계정", registered ? "연결된 계정 관리" : "예약을 위한 계정 연결")}
      <section class="notice calm"><b>자격증명은 서버에서만 사용합니다.</b><p>앱은 코레일 비밀번호를 저장하지 않으며, 서버 응답에도 비밀번호가 포함되지 않습니다.</p></section>
      ${registered ? `<section class="card"><div class="account-state"><span>✓</span><div><b>코레일 계정 연결됨</b><small>열차 조회와 예약을 시작할 수 있어요.</small></div></div><button class="button ghost danger" data-action="rail-logout">코레일 계정 연결 해제</button></section>` : `<form id="rail-form" class="form-stack"><label class="field"><span>휴대전화 번호 또는 회원번호</span><input name="username" inputmode="tel" maxlength="128" autocomplete="username" required></label><label class="field"><span>코레일 비밀번호</span><input name="password" type="password" maxlength="128" autocomplete="current-password" required></label>${this.state!.capabilities.korail ? "" : '<p class="availability center">예약 서버가 연결되지 않아 코레일 계정을 확인할 수 없어요.</p>'}${this.renderError()}<button class="button primary" type="submit" ${this.state!.capabilities.korail ? "" : "disabled"}>계정 확인하고 연결</button></form>`}`;
  }

  private renderAuth(): string {
    if (this.options.demoMode) {
      return `<div class="auth-shell with-demo"><aside class="demo-banner" data-demo-banner aria-label="데모 상태"><b>데모 모드</b><span>실제 조회·예약 없음</span></aside><div class="auth-art"><span class="orbit one"></span><span class="orbit two"></span><i>틈</i></div><section class="auth-card"><p class="eyebrow">FIXTURE ONLY</p><h1>자격증명 없이\n둘러보세요.</h1><p>데모는 샘플 데이터만 사용하며 로그인이나 철도 요청을 보내지 않습니다.</p><button class="button primary" data-action="demo-enter">샘플 화면 시작 <span>→</span></button></section></div>`;
    }
    const register = this.authMode === "register";
    const admin = this.authGate === "admin";
    const choosing = this.authGate === "choose";
    const title = choosing
      ? "누구로\n들어오세요."
      : admin
        ? "관리자로\n들어갑니다."
        : register
          ? "초대받은 틈으로\n들어오세요."
          : "선택받은 자로\n이어갑니다.";
    const copy = choosing
      ? "관리자는 아이디와 비밀번호로, 선택받은 자는 초대 코드로 처음 계정을 만듭니다."
      : admin
        ? "운영자 아이디와 비밀번호를 입력하세요."
        : register
          ? "받은 코드로 아이디와 비밀번호를 정합니다."
          : "이미 만든 아이디와 비밀번호로 들어옵니다.";
    return `<div class="auth-shell ${this.options.demoMode ? "with-demo" : ""}">
      ${this.options.demoMode ? '<aside class="demo-banner" data-demo-banner aria-label="데모 상태"><b>데모 모드</b><span>실제 조회·예약 없음</span></aside>' : ""}
      <div class="auth-art"><span class="orbit one"></span><span class="orbit two"></span><i>틈</i></div>
      <section class="auth-card"><p class="eyebrow">A LITTLE SPACE FOR YOUR JOURNEY</p><h1>${title}</h1><p>${copy}</p>
        ${choosing ? `<div class="gate-list"><button class="gate-card" data-auth-gate="admin" type="button"><b>관리자</b><small>아이디와 비밀번호로 들어갑니다</small></button><button class="gate-card" data-auth-gate="guest" type="button"><b>선택받은 자</b><small>로그인하거나 초대 코드로 처음 설정합니다</small></button></div>` : ""}
        ${admin ? `<form id="auth-form" class="form-stack"><label class="field"><span>관리자 아이디</span><input name="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_]{3,32}" autocomplete="username" required></label><label class="field"><span>관리자 비밀번호</span><input name="password" type="password" minlength="12" maxlength="128" autocomplete="current-password" required></label>${this.renderError()}<button class="button primary" type="submit">관리자로 들어가기 <span>→</span></button></form><button class="text-button" data-auth-gate="choose" type="button">다른 방법으로</button>` : ""}
        ${this.authGate === "guest" ? `<div class="segmented"><button data-auth-mode="login" class="${register ? "" : "active"}" type="button">로그인</button><button data-auth-mode="register" class="${register ? "active" : ""}" type="button">처음 설정</button></div>
        <form id="auth-form" class="form-stack">
          <label class="field"><span>앱 아이디</span><input name="username" minlength="3" maxlength="32" pattern="[A-Za-z0-9_]{3,32}" autocomplete="username" required></label>
          <label class="field"><span>앱 비밀번호</span><input name="password" type="password" minlength="12" maxlength="128" autocomplete="${register ? "new-password" : "current-password"}" required></label>
          ${register ? '<label class="field"><span>최초 설정용 코드</span><input name="invite" minlength="16" maxlength="128" autocomplete="one-time-code" required></label>' : ""}
          ${this.renderError()}
          <button class="button primary" type="submit">${register ? "계정 만들고 시작하기" : "로그인"} <span>→</span></button>
        </form>
        <button class="text-button" data-auth-gate="choose" type="button">다른 방법으로</button>` : ""}
        ${this.options.demoMode ? '<button class="text-button demo-enter" data-action="demo-enter">샘플 화면 바로 보기</button>' : ""}
        <p class="auth-note">앱 계정과 코레일 계정은 서로 다릅니다. 결제는 코레일에서 직접 진행합니다.</p>
      </section>
      ${this.busy ? '<div class="blocker" role="status"><span class="spinner"></span><b>확인하고 있어요</b></div>' : ""}
    </div>`;
  }

  private renderUnavailable(): string {
    return `<div class="empty"><span>!</span><h2>앱을 열지 못했어요</h2><p>${escapeHtml(this.error || "서버 상태를 확인할 수 없습니다.")}</p><button class="button primary" data-action="reload">다시 시도</button></div>`;
  }

  private renderNavigation(): string {
    const items: Array<[AppView, string, string]> = [
      ["home", "⌁", "홈"],
      ["activity", "◉", "내 예약"],
      ["favourites", "☆", "즐겨찾기"],
      ["settings", "○", "설정"],
    ];
    return `<nav class="bottom-nav" aria-label="주요 메뉴">${items.map(([view, icon, label]) => `<button data-view="${view}" class="${this.view === view ? "active" : ""}"><b>${icon}</b><span>${label}</span></button>`).join("")}</nav>`;
  }

  private renderSubhead(eyebrow: string, title: string): string {
    return `<header class="subhead"><button class="icon-button" data-action="back" aria-label="뒤로">←</button><div><p class="eyebrow">${escapeHtml(eyebrow)}</p><h1>${escapeHtml(title)}</h1></div></header>`;
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
      seatOption: (["1", "2", "3", "4"].includes(value("seat_option"))
        ? value("seat_option")
        : "1") as BookingDraft["seatOption"],
      passengerCount: this.draft.passengerCount,
      seatStrategy: data.has("seat_strategy")
        ? value("seat_strategy") === "2" ? "2" : "1"
        : this.draft.seatStrategy,
      seatColumns: data.getAll("seat_column").map(String),
      seatRowMin: value("seat_row_min"),
      seatRowMax: value("seat_row_max"),
    };
  }

  private validateDraft(draft: BookingDraft): string | null {
    if (!draft.depDate || !draft.srcStation || !draft.dstStation || !draft.depTime) {
      return "구간과 날짜, 출발 시각을 모두 입력해주세요.";
    }
    if (draft.srcStation === draft.dstStation) return "출발역과 도착역은 달라야 합니다.";
    if (!draft.unlimitedTime && (!draft.maxDepTime || draft.maxDepTime <= draft.depTime)) {
      return "검색 종료 시각은 시작 시각보다 늦어야 합니다.";
    }
    const low = draft.seatRowMin ? Number(draft.seatRowMin) : null;
    const high = draft.seatRowMax ? Number(draft.seatRowMax) : null;
    if ((low !== null && (low < 1 || low > 99)) || (high !== null && (high < 1 || high > 99))) {
      return "좌석 번호는 1부터 99 사이로 입력해주세요.";
    }
    if (low !== null && high !== null && low > high) {
      return "첫 좌석 번호는 마지막 번호보다 클 수 없습니다.";
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
      this.navigate("trains");
    });
  }

  private async startNow(): Promise<void> {
    const conditions = this.conditions ?? buildConditions(this.draft);
    await this.run(async (isCurrent) => {
      const result = await this.api.search(buildBookingPayload(conditions, this.selectedTrains));
      if (!isCurrent()) return;
      if (result.needsAccessRequest) {
        this.accessRequired = true;
        this.accessRequestPending = result.accessRequestPending === true;
        this.error = result.accessRequestPending
          ? "이미 사용 승인을 요청했습니다. 운영자의 답을 기다리는 중입니다."
          : "체험 횟수를 모두 사용했습니다. 아래에서 운영자에게 사용 승인을 요청할 수 있어요.";
        return;
      }
      if (!result.started) {
        this.error = "서버가 검색 시작을 확인하지 않았습니다.";
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
      this.error = "검색을 시작할 시각을 골라주세요.";
      this.render();
      return;
    }
    const selected = new Date(input.value);
    if (Number.isNaN(selected.getTime())) {
      this.error = "검색 시작 시각을 읽을 수 없습니다.";
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
        this.error = "서버가 시작 예약을 확인하지 않았습니다.";
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
        this.error = "서버가 즐겨찾기 저장을 확인하지 않았습니다.";
        return;
      }
      this.state!.favourites = result.favourites;
      this.showToast("즐겨찾기에 저장했어요.");
      this.render();
    });
  }

  private async loadNotifications(): Promise<void> {
    if (!this.state?.capabilities.durableNotifications) return;
    await this.run(async (isCurrent) => {
      const result = await this.api.notifications();
      if (!isCurrent()) return;
      this.notifications = result.items;
      this.state!.pushAvailable = result.pushAvailable;
      this.render();
    });
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
        this.error = "로그인이 만료되었습니다. 다시 로그인해주세요.";
        this.render();
        return;
      }
      if (error.kind === "offline") this.connection = "offline";
      this.error = error.message;
      return;
    }
    this.error = error instanceof Error ? error.message : "문제가 생겼습니다. 다시 시도해주세요.";
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
    const train = button.dataset.train;
    if (train) {
      this.selectedTrains = this.selectedTrains.includes(train)
        ? this.selectedTrains.filter((number) => number !== train)
        : [...this.selectedTrains, train];
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
        void this.reload();
        break;
      case "demo-enter":
        this.view = "home";
        void this.reload();
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
        this.navigate("confirm");
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
              ? "이 앱 계정은 이미 사용 승인이 되어 있어요."
              : result.requested
                ? "운영자에게 사용 승인을 요청했어요."
                : "사용 승인 요청 상태를 확인해주세요.",
          );
        });
        break;
      case "cancel-search":
        if (window.confirm("진행 중인 검색 또는 시작 예약을 취소할까요?")) {
          void this.run(async (isCurrent) => {
            const result = await this.api.cancelSearch();
            if (!isCurrent()) return;
            if (!result.stopped && !result.unscheduled) {
              this.error = "서버에서 취소할 검색을 찾지 못했습니다.";
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
              this.error = "서버가 예약 취소를 확인하지 않았습니다.";
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
        window.localStorage.setItem("teum.theme", this.theme);
        this.render();
        break;
      case "request-push":
        void this.options.onRequestPush?.();
        break;
      case "create-invite":
        void this.run(async (isCurrent) => {
          const result = await this.api.createInvite({ ttlHours: 72 });
          if (!isCurrent()) return;
          this.invitePreview = result.invite;
          this.showToast("초대 코드를 만들었어요. 지금 화면에만 다시 보입니다.");
        });
        break;
      case "copy-invite":
        if (this.invitePreview) {
          void navigator.clipboard?.writeText(this.invitePreview).then(
            () => this.showToast("초대 코드를 복사했어요."),
            () => this.showToast("복사를 지원하지 않는 기기입니다. 코드를 직접 전달해주세요."),
          );
        }
        break;
      case "dismiss-invite":
        this.invitePreview = "";
        this.render();
        break;
      case "rail-logout":
        if (window.confirm("코레일 계정 연결을 해제할까요? 진행 중인 검색은 별도로 중지해야 합니다.")) {
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
      if (target.name === "unlimited_time") this.render();
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
        this.error = "서버가 로그인 세션을 발급하지 않았습니다.";
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
        this.error = "서버가 코레일 계정 연결을 확인하지 않았습니다.";
        return;
      }
      this.state!.rail.registered = true;
      this.showToast("코레일 계정을 연결했어요.");
      this.view = "settings";
    });
  }

  private async changeNotify(direction: number): Promise<void> {
    const current = NOTIFY_STEPS.indexOf(this.state!.notifyMinutes);
    const next = Math.min(NOTIFY_STEPS.length - 1, Math.max(0, current + direction));
    const minutes = NOTIFY_STEPS[next]!;
    await this.run(async (isCurrent) => {
      const result = await this.api.setNotify(minutes);
      if (!isCurrent()) return;
      this.state!.notifyMinutes = result.notifyMinutes;
      this.showToast(result.notifyMinutes ? `${result.notifyMinutes}분마다 알려드려요.` : "검색 진행 알림을 껐어요.");
    });
  }
}
