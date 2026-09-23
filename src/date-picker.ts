// 앱 테마를 따르는 날짜 선택기. Android WebView 의 기본 달력은 앱 테마를 무시해서 직접 그려요.
// 값은 숨은 입력에 YYYY-MM-DD 로 두고, 고를 때마다 change 이벤트를 올려 기존 폼·시트 흐름을 그대로 써요.

import { esc } from "./ui/html";

const WEEKDAYS = ["일", "월", "화", "수", "목", "금", "토"];

// ---- 날짜 계산: 모두 "YYYY-MM-DD" 문자열로 주고받고, 시간대가 끼지 않게 UTC 로만 셈해요. ----

function parts(iso: string): [number, number, number] {
  const [year = 0, month = 1, day = 1] = iso.split("-").map(Number);
  return [year, month, day];
}

// 달·일이 넘치면 Date 가 알아서 다음 달로 넘겨요(9월 31일 → 10월 1일).
export function isoFromParts(year: number, month: number, day: number): string {
  return new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10);
}

export function isIsoDate(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value) && isoFromParts(...parts(value)) === value;
}

export function localIso(date = new Date()): string {
  return isoFromParts(date.getFullYear(), date.getMonth() + 1, date.getDate());
}

export function addDays(iso: string, days: number): string {
  const [year, month, day] = parts(iso);
  return isoFromParts(year, month, day + days);
}

function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

// 1월 31일에서 한 달 뒤는 2월 마지막 날이에요. 달을 건너뛰지 않아요.
export function addMonths(iso: string, months: number): string {
  const [year, month, day] = parts(iso);
  const first = isoFromParts(year, month + months, 1);
  const [nextYear, nextMonth] = parts(first);
  return isoFromParts(nextYear, nextMonth, Math.min(day, daysInMonth(nextYear, nextMonth)));
}

export function weekday(iso: string): number {
  const [year, month, day] = parts(iso);
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay();
}

/** 서버가 받는 출발일: 오늘부터 1년(365일) 뒤까지. backend validators.validate_date 와 같은 규칙이에요. */
export function departureRange(now = new Date()): { min: string; max: string } {
  const min = localIso(now);
  return { min, max: addDays(min, 365) };
}

/** 일요일부터 시작하는 6주(42칸) 달력. 그 달이 아닌 칸은 null 이라 달마다 높이가 같아요. */
export function monthGrid(month: string): Array<string | null> {
  const [year, monthNo] = parts(`${month}-01`);
  const lead = weekday(`${month}-01`);
  const count = daysInMonth(year, monthNo);
  return Array.from({ length: 42 }, (_, index) => {
    const day = index - lead + 1;
    return day >= 1 && day <= count ? isoFromParts(year, monthNo, day) : null;
  });
}

/** 방향키가 옮길 날짜. 고를 수 있는 범위 밖으로는 나가지 않고, 다루지 않는 키면 null. */
export function moveFocus(iso: string, key: string, min: string, max: string): string | null {
  const moves: Record<string, () => string> = {
    ArrowLeft: () => addDays(iso, -1),
    ArrowRight: () => addDays(iso, 1),
    ArrowUp: () => addDays(iso, -7),
    ArrowDown: () => addDays(iso, 7),
    Home: () => addDays(iso, -weekday(iso)),
    End: () => addDays(iso, 6 - weekday(iso)),
    PageUp: () => addMonths(iso, -1),
    PageDown: () => addMonths(iso, 1),
  };
  const next = moves[key]?.();
  if (!next) return null;
  return next < min ? min : next > max ? max : next;
}

export function fullDateLabel(iso: string): string {
  const [year, month, day] = parts(iso);
  return `${year}년 ${month}월 ${day}일 ${WEEKDAYS[weekday(iso)]}요일`;
}

export function shortDateLabel(iso: string): string {
  const [, month, day] = parts(iso);
  return `${month}월 ${day}일 (${WEEKDAYS[weekday(iso)]})`;
}

function monthTitle(month: string): string {
  const [year, monthNo] = parts(`${month}-01`);
  return `${year}년 ${monthNo}월`;
}

// ---- 화면 조각 ----

export interface DatePickerOptions {
  /** 숨은 입력의 id 이자 달력 상태의 열쇠. */
  id: string;
  /** 폼에 실어 보낼 이름(dep_date). 폼 밖이면 비워 둬요. */
  name?: string;
  value: string;
  label: string;
  min: string;
  max: string;
  /** 폼 안에서는 날짜 칸을 눌러 펼치고, 고르면 다시 접어요. 시트에서는 늘 펼쳐 둬요. */
  collapsible?: boolean;
}

interface PickerState {
  options: DatePickerOptions;
  value: string;
  month: string;
  focus: string;
  open: boolean;
  // 방금 펼쳤을 때 한 번만 움직여요. 앱이 화면을 다시 그릴 때마다 되풀이되지 않게요.
  entering: boolean;
}


const CHEVRON = (path: string) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`;

/**
 * 앱 루트 하나에 붙는 날짜 선택기. 앱은 render() 가 돌려준 HTML 을 화면에 넣고, 화면을 통째로 다시 그린 뒤 refresh() 를 불러요.
 * 보고 있는 달·펼침 여부는 여기에 남아서 앱의 전체 다시 그리기를 견뎌요.
 */
export class DatePicker {
  private readonly states = new Map<string, PickerState>();

  constructor(private readonly root: HTMLElement) {
    this.onClick = this.onClick.bind(this);
    this.onKeyDown = this.onKeyDown.bind(this);
    this.onChange = this.onChange.bind(this);
    root.addEventListener("click", this.onClick);
    root.addEventListener("keydown", this.onKeyDown);
    root.addEventListener("change", this.onChange);
  }

  dispose(): void {
    this.root.removeEventListener("click", this.onClick);
    this.root.removeEventListener("keydown", this.onKeyDown);
    this.root.removeEventListener("change", this.onChange);
    this.states.clear();
  }

  reset(): void {
    this.states.clear();
  }

  /** 새로 여는 시트처럼 지난번에 보던 달을 잊고 값의 달부터 보여 줘야 할 때. */
  forget(id: string): void {
    this.states.delete(id);
  }

  render(options: DatePickerOptions): string {
    const value = isIsoDate(options.value) ? options.value : "";
    let state = this.states.get(options.id);
    // 값이 바깥에서 바뀌었으면(새 여정, 즐겨찾기 불러오기) 그 날짜의 달부터 접힌 채로 다시 시작해요.
    if (!state || state.value !== value) {
      state = { options, value, month: (value || options.min).slice(0, 7), focus: value || options.min, open: false, entering: false };
      this.states.set(options.id, state);
    }
    state.options = options;
    return this.markup(state);
  }

  /** 앱이 화면을 다시 그린 뒤: 숨은 입력에 되살려 둔 값을 받아들이고 달력을 그 값에 맞춰요. */
  refresh(): void {
    for (const element of this.root.querySelectorAll<HTMLElement>("[data-date-picker]")) {
      const state = this.states.get(element.dataset.datePicker ?? "");
      const input = element.querySelector<HTMLInputElement>("input");
      if (!state || !input) continue;
      this.adopt(state, input.value);
      this.update(state, "none");
    }
  }

  private adopt(state: PickerState, value: string): void {
    if (value === state.value || (value && !isIsoDate(value))) return;
    state.value = value;
    state.month = (value || state.options.min).slice(0, 7);
    state.focus = value || state.options.min;
  }

  private markup(state: PickerState): string {
    const { id, name, label, collapsible } = state.options;
    const today = localIso();
    const toggle = collapsible
      ? `<button type="button" class="date-field" data-dp="toggle" aria-expanded="${state.open}" ${state.open ? `aria-controls="${esc(id)}-calendar"` : ""} aria-label="${esc(`${label} ${state.value ? fullDateLabel(state.value) : "선택 안 함"}`)}">
          <svg class="date-field-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3.5" y="5" width="17" height="15" rx="3.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/></svg>
          <b>${state.value ? shortDateLabel(state.value) : "날짜를 골라 주세요"}</b>
          ${state.value === today ? "<em>오늘</em>" : state.value === addDays(today, 1) ? "<em>내일</em>" : ""}
          ${CHEVRON("m6 9 6 6 6-6")}
        </button>`
      : "";
    return `<div class="date-picker ${collapsible ? "collapsible" : ""} ${state.open ? "open" : ""}" data-date-picker="${esc(id)}">
      <input type="hidden" id="${esc(id)}" ${name ? `name="${esc(name)}"` : ""} value="${state.value}">
      ${toggle}
      ${!collapsible || state.open ? this.calendar(state, today) : ""}
    </div>`;
  }

  private calendar(state: PickerState, today: string): string {
    const { id, label, min, max } = state.options;
    const days = monthGrid(state.month);
    const enabled = (day: string) => day >= min && day <= max;
    // 방향키로 들어올 한 칸: 옮겨 둔 칸, 고른 날, 오늘, 그 달의 첫 고를 수 있는 날 순서예요.
    const tabStop = [state.focus, state.value, today].find((day) => day?.startsWith(state.month) && enabled(day))
      ?? days.find((day): day is string => day !== null && enabled(day));
    const rows = Array.from({ length: 6 }, (_, week) => `<tr>${days.slice(week * 7, week * 7 + 7).map((day, column) => {
      if (!day) return '<td role="gridcell"></td>';
      const selected = day === state.value;
      const classes = ["date-day", column === 0 ? "sun" : column === 6 ? "sat" : "", day === today ? "today" : "", selected ? "selected" : ""].filter(Boolean).join(" ");
      return `<td role="gridcell" aria-selected="${selected}"><button type="button" class="${classes}" data-dp-day="${day}" tabindex="${day === tabStop ? 0 : -1}" aria-label="${fullDateLabel(day)}${day === today ? ", 오늘" : ""}" ${day === today ? 'aria-current="date"' : ""} ${enabled(day) ? "" : "disabled"}><span>${Number(day.slice(8))}</span></button></td>`;
    }).join("")}</tr>`).join("");
    return `<div class="date-calendar ${state.entering ? "entering" : ""}" id="${esc(id)}-calendar" role="group" aria-label="${esc(label)} 달력">
      <div class="date-calendar-head">
        <button type="button" class="date-nav" data-dp="prev" aria-label="이전 달" ${state.month <= min.slice(0, 7) ? "disabled" : ""}>${CHEVRON("m15 18-6-6 6-6")}</button>
        <h3 id="${esc(id)}-month" aria-live="polite">${monthTitle(state.month)}</h3>
        <button type="button" class="date-nav" data-dp="next" aria-label="다음 달" ${state.month >= max.slice(0, 7) ? "disabled" : ""}>${CHEVRON("m9 18 6-6-6-6")}</button>
      </div>
      <table class="date-grid" role="grid" aria-labelledby="${esc(id)}-month">
        <thead><tr>${WEEKDAYS.map((day, column) => `<th scope="col" abbr="${day}요일" class="${column === 0 ? "sun" : column === 6 ? "sat" : ""}">${day}</th>`).join("")}</tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
  }

  /** 이 달력 하나만 다시 그려요. focus 는 다시 그린 뒤 어디에 초점을 둘지예요. */
  private update(state: PickerState, focus: "none" | "keep" | "day" | "toggle"): void {
    const element = this.element(state);
    if (!element) return;
    const active = document.activeElement instanceof HTMLElement && element.contains(document.activeElement)
      ? document.activeElement.dataset.dp
      : undefined;
    const template = document.createElement("template");
    template.innerHTML = this.markup(state);
    const replacement = template.content.firstElementChild as HTMLElement;
    element.replaceWith(replacement);
    state.entering = false;
    const target = focus === "toggle"
      ? replacement.querySelector<HTMLElement>('[data-dp="toggle"]')
      : focus === "day"
        ? replacement.querySelector<HTMLElement>('[data-dp-day][tabindex="0"]')
        : focus === "keep" && active
          ? replacement.querySelector<HTMLButtonElement>(`[data-dp="${active}"]:not(:disabled)`) ?? replacement.querySelector<HTMLElement>('[data-dp-day][tabindex="0"]')
          : null;
    target?.focus();
  }

  // id 는 앱이 정한 상수라 따옴표가 들어가지 않아요.
  private element(state: PickerState): HTMLElement | null {
    return this.root.querySelector<HTMLElement>(`[data-date-picker="${state.options.id}"]`);
  }

  private stateOf(element: Element): PickerState | undefined {
    return this.states.get(element.closest<HTMLElement>("[data-date-picker]")?.dataset.datePicker ?? "");
  }

  private onClick(event: Event): void {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-date-picker] button");
    const state = button && this.stateOf(button);
    if (!button || !state) return;
    const day = button.dataset.dpDay;
    if (day) {
      state.value = day;
      state.focus = day;
      if (state.options.collapsible) state.open = false;
      this.update(state, state.options.collapsible ? "toggle" : "day");
      this.element(state)?.querySelector("input")?.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    const action = button.dataset.dp;
    if (action === "toggle") {
      state.open = !state.open;
      state.entering = state.open;
      if (state.open) {
        state.month = (state.value || state.options.min).slice(0, 7);
        state.focus = state.value || state.options.min;
      }
      this.update(state, "toggle");
      this.element(state)?.querySelector(".date-calendar")?.scrollIntoView?.({ block: "nearest", behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    } else if (action === "prev" || action === "next") {
      state.month = addMonths(`${state.month}-01`, action === "prev" ? -1 : 1).slice(0, 7);
      this.update(state, "keep");
    }
  }

  private onKeyDown(event: KeyboardEvent): void {
    const target = event.target as HTMLElement;
    const state = target.closest?.("[data-date-picker]") ? this.stateOf(target) : undefined;
    if (!state) return;
    if (event.key === "Escape" && state.options.collapsible && state.open) {
      event.preventDefault();
      state.open = false;
      this.update(state, "toggle");
      return;
    }
    const day = target.dataset.dpDay;
    const next = day ? moveFocus(day, event.key, state.options.min, state.options.max) : null;
    if (!next) return;
    event.preventDefault();
    state.focus = next;
    state.month = next.slice(0, 7);
    this.update(state, "day");
  }

  // 오늘·내일 칩처럼 바깥에서 숨은 입력 값을 바꾸고 change 를 올리면 달력이 따라가요.
  private onChange(event: Event): void {
    const input = event.target as HTMLInputElement;
    const state = input.closest?.("[data-date-picker]") ? this.stateOf(input) : undefined;
    if (!state || input.value === state.value) return;
    this.adopt(state, input.value);
    this.update(state, "none");
  }
}
