// 앱 테마를 따르는 시간 선택기. Android WebView 의 기본 시계는 앱 테마를 무시해서 직접 그려요.
// 검색 시간대는 대략 고르니 정시만 골라요. 펼치면 시각이 위아래로 넘어가는 휠이 나오고, 가운데 줄이 고른 시각이에요.
// 값은 숨은 입력에 HH:MM(분은 00)으로 두고, 바뀔 때마다 change 이벤트를 올려 기존 폼 흐름을 그대로 써요.

import { attrs, cx, esc } from "./ui/html";

/** 휠 한 줄의 높이(px). 손가락으로 누르는 줄이라 48px 이에요. CSS 의 --time-row 와 같아요. */
export const ROW_HEIGHT = 48;
// 손을 뗀 뒤 스크롤이 이만큼 멈춰 있으면 멈춘 것으로 봐요. scrollend 를 모르는 WebView 를 위한 몫이에요.
const SETTLE_MS = 150;

// ---- 시각 계산: 모두 "HH:MM" 문자열로 주고받아요. ----

export function isClock(value: string): boolean {
  return /^([01]\d|2[0-3]):[0-5]\d$/.test(value);
}

function parts(value: string): [number, number] {
  const [hour = 0, minute = 0] = value.split(":").map(Number);
  return [hour, minute];
}

const pad = (value: number) => String(value).padStart(2, "0");

/** "13:05" → "오후 1:05". 자정은 "오전 12:00", 정오는 "오후 12:00" 이에요. */
export function clockLabel(value: string): string {
  const [hour, minute] = parts(value);
  return `${hour < 12 ? "오전" : "오후"} ${hour % 12 || 12}:${pad(minute)}`;
}

/** 7 → "오전 7시". 휠의 한 줄이 이렇게 읽혀요. */
export function hourLabel(hour: number): string {
  return `${hour < 12 ? "오전" : "오후"} ${hour % 12 || 12}시`;
}

/** 칸에 보이는 시각. 정시는 휠처럼 "오전 7시", 예전에 분까지 저장한 값(05:57)은 그대로 "오전 5:57" 이에요. */
export function timeLabel(value: string): string {
  const [hour, minute] = parts(value);
  return minute ? clockLabel(value) : hourLabel(hour);
}

/** 그 시의 정시. 7 → "07:00". */
export function hourClock(hour: number): string {
  return `${pad(hour)}:00`;
}

// ---- 화면 조각 ----

export interface TimePickerOptions {
  /** 숨은 입력의 id 이자 펼침 상태의 열쇠. */
  id: string;
  /** 폼에 실어 보낼 이름(dep_time). 폼 밖이면 비워 둬요. */
  name?: string;
  value: string;
  label: string;
  /** 마지막 열차까지 찾을 때의 "이 시간까지"처럼 쓰지 않는 칸. 숨은 입력도 꺼져 폼에 실리지 않아요. */
  disabled?: boolean;
  /** 고를 수 있는 시(처음, 끝). 기본은 열차가 다니는 5–23시예요. */
  hours?: [number, number];
}

interface PickerState {
  options: TimePickerOptions;
  value: string;
  open: boolean;
  // 방금 펼쳤을 때 한 번만 움직여요. 앱이 화면을 다시 그릴 때마다 되풀이되지 않게요.
  entering: boolean;
}

const CLOCK = '<svg class="date-field-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>';
const CHEVRON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';

/**
 * 앱 루트 하나에 붙는 시간 선택기. 날짜 선택기(DatePicker)와 같은 방식으로 앱의 전체 다시 그리기를 견뎌요.
 * 앱은 render() 가 돌려준 HTML 을 화면에 넣고, 다시 그린 뒤 refresh() 를 불러요.
 *
 * 휠을 손으로 넘기다 멈추면 가운데 줄의 정시가 값이 돼요. 넘기는 동안에는 칸을 닫지 않아요.
 * 한 줄을 누르거나(방향키로 옮긴 뒤 Enter) 고르면 곧장 다음으로 넘어가요: (나란한 두 칸의 앞 칸이면) 뒤 칸 → 닫기.
 */
export class TimePicker {
  private readonly states = new Map<string, PickerState>();
  // 손가락·마우스로 만진 휠. 앱이 휠을 제자리에 돌려놓을 때의 스크롤은 고른 것이 아니라서, 만진 휠만 값을 바꿔요.
  private touched: HTMLElement | null = null;
  private settleTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(private readonly root: HTMLElement) {
    this.onClick = this.onClick.bind(this);
    this.onKeyDown = this.onKeyDown.bind(this);
    this.onPointerDown = this.onPointerDown.bind(this);
    this.onScroll = this.onScroll.bind(this);
    this.onScrollEnd = this.onScrollEnd.bind(this);
    root.addEventListener("click", this.onClick);
    root.addEventListener("keydown", this.onKeyDown);
    root.addEventListener("pointerdown", this.onPointerDown);
    // 마우스 휠·터치패드로 넘겨도 만진 거예요.
    root.addEventListener("wheel", this.onPointerDown, { passive: true });
    // scroll 은 거품처럼 올라오지 않아서 내려가는 길(capture)에서 들어요.
    root.addEventListener("scroll", this.onScroll, true);
    root.addEventListener("scrollend", this.onScrollEnd, true);
  }

  dispose(): void {
    this.root.removeEventListener("click", this.onClick);
    this.root.removeEventListener("keydown", this.onKeyDown);
    this.root.removeEventListener("pointerdown", this.onPointerDown);
    this.root.removeEventListener("wheel", this.onPointerDown);
    this.root.removeEventListener("scroll", this.onScroll, true);
    this.root.removeEventListener("scrollend", this.onScrollEnd, true);
    clearTimeout(this.settleTimer);
    this.states.clear();
    this.touched = null;
  }

  reset(): void {
    this.states.clear();
  }

  /** 새로 여는 패널처럼 지난번 펼침 상태를 잊어야 할 때. */
  forget(id: string): void {
    this.states.delete(id);
  }

  render(options: TimePickerOptions): string {
    const value = isClock(options.value) ? options.value : "";
    let state = this.states.get(options.id);
    // 값이 바깥에서 바뀌었으면(새 여정, 즐겨찾기 불러오기) 접힌 채로 다시 시작해요.
    if (!state || state.value !== value) {
      state = { options, value, open: false, entering: false };
      this.states.set(options.id, state);
    }
    state.options = options;
    if (options.disabled) state.open = false;
    return this.markup(state);
  }

  /** 앱이 화면을 다시 그린 뒤: 숨은 입력에 되살려 둔 값이 다르면 받아들이고, 펼친 휠을 고른 시각에 맞춰 둬요. */
  refresh(): void {
    for (const element of this.root.querySelectorAll<HTMLElement>("[data-time-picker]")) {
      const state = this.states.get(element.dataset.timePicker ?? "");
      if (!state) continue;
      const value = element.querySelector<HTMLInputElement>("input[type=hidden]")?.value ?? "";
      if (value !== state.value && !(value && !isClock(value))) {
        state.value = value;
        this.update(state, null);
      } else if (state.open) {
        // 새로 그린 휠은 맨 위에서 시작해요.
        this.center(state, "auto");
      }
    }
  }

  private hours(state: PickerState): number[] {
    const [from, to] = state.options.hours ?? [5, 23];
    return Array.from({ length: to - from + 1 }, (_, index) => from + index);
  }

  private hourOf(state: PickerState): number | null {
    return state.value ? parts(state.value)[0] : null;
  }

  /** 휠 가운데에 둘 줄. 고른 시각이 없으면 지금 시각이에요(범위 밖이면 가까운 끝). */
  private centerIndex(state: PickerState): number {
    const hours = this.hours(state);
    const hour = this.hourOf(state) ?? new Date().getHours();
    return Math.min(hours.length - 1, Math.max(0, hour - hours[0]!));
  }

  private fieldText(state: PickerState): string {
    return state.value ? timeLabel(state.value) : "시각을 골라 주세요";
  }

  private fieldName(state: PickerState): string {
    return `${state.options.label} ${state.value ? timeLabel(state.value) : "선택 안 함"}`;
  }

  private optionId(state: PickerState, hour: number): string {
    return `${state.options.id}-h${hour}`;
  }

  private markup(state: PickerState): string {
    const { id, name, disabled } = state.options;
    return `<div class="${cx("time-picker", state.open && "open")}" data-time-picker="${esc(id)}">
      <input ${attrs({ type: "hidden", id, name, value: state.value, disabled })}>
      <button ${attrs({
        type: "button",
        class: "date-field time-field",
        "data-tp": "toggle",
        "aria-expanded": state.open,
        "aria-controls": state.open ? `${id}-panel` : null,
        "aria-label": this.fieldName(state),
        disabled,
      })}>${CLOCK}<b>${esc(this.fieldText(state))}</b>${CHEVRON}</button>
      ${state.open ? this.panel(state) : ""}
    </div>`;
  }

  private panel(state: PickerState): string {
    const { id, label } = state.options;
    const hours = this.hours(state);
    const chosen = this.hourOf(state);
    const current = hours[this.centerIndex(state)]!;
    const options = hours.map((hour) => `<div ${attrs({
      id: this.optionId(state, hour),
      class: cx("time-option", hour === current && "current"),
      role: "option",
      "aria-selected": hour === chosen,
      "data-tp-hour": hour,
    })}>${esc(hourLabel(hour))}</div>`).join("");
    return `<div class="${cx("time-panel", state.entering && "entering")}" id="${esc(id)}-panel" role="group" aria-label="${esc(label)} 고르기">
      <p class="time-panel-cap">${esc(label)} 고르는 중</p>
      <div class="time-wheel"><div ${attrs({
        class: "time-wheel-list",
        role: "listbox",
        tabindex: 0,
        "aria-label": label,
        "aria-activedescendant": this.optionId(state, current),
        "data-tp-wheel": "",
      })}>${options}</div></div>
    </div>`;
  }

  /** 이 선택기 하나만 다시 그리고, focus 선택자가 있으면 그곳에 초점을 둬요. */
  private update(state: PickerState, focus: string | null): void {
    const element = this.element(state);
    if (!element) return;
    const template = document.createElement("template");
    template.innerHTML = this.markup(state);
    const replacement = template.content.firstElementChild as HTMLElement;
    element.replaceWith(replacement);
    state.entering = false;
    if (state.open) this.center(state, "auto");
    if (focus) replacement.querySelector<HTMLElement>(focus)?.focus({ preventScroll: true });
  }

  /** 넘기는 동안에는 통째로 다시 그리지 않고 글자·값만 고쳐요. 바꿔 끼우면 손가락 밑의 휠이 사라져요. */
  private paint(state: PickerState): void {
    const element = this.element(state);
    if (!element) return;
    element.querySelector<HTMLInputElement>("input[type=hidden]")!.value = state.value;
    const toggle = element.querySelector<HTMLElement>("[data-tp='toggle']")!;
    toggle.setAttribute("aria-label", this.fieldName(state));
    toggle.querySelector("b")!.textContent = this.fieldText(state);
    const chosen = this.hourOf(state);
    for (const option of element.querySelectorAll<HTMLElement>("[data-tp-hour]")) {
      option.setAttribute("aria-selected", String(Number(option.dataset.tpHour) === chosen));
    }
  }

  /** 가운데 줄의 글자를 돋보이게 하고 보조 기기에 알려요. 고르는 것과는 따로예요. */
  private mark(wheel: HTMLElement, index: number): void {
    const options = wheel.querySelectorAll<HTMLElement>("[data-tp-hour]");
    options.forEach((option, at) => option.classList.toggle("current", at === index));
    const current = options[index];
    if (current) wheel.setAttribute("aria-activedescendant", current.id);
  }

  private wheelOf(state: PickerState): HTMLElement | null {
    return this.element(state)?.querySelector<HTMLElement>("[data-tp-wheel]") ?? null;
  }

  private rowHeight(wheel: HTMLElement): number {
    return wheel.querySelector<HTMLElement>("[data-tp-hour]")?.offsetHeight || ROW_HEIGHT;
  }

  /** 휠이 지금 가운데에 둔 줄. 위아래 여백이 두 줄이라 n 번째 줄은 scrollTop 이 n 줄일 때 가운데예요. */
  private centeredIndex(state: PickerState, wheel: HTMLElement): number {
    const index = Math.round(wheel.scrollTop / this.rowHeight(wheel));
    return Math.min(this.hours(state).length - 1, Math.max(0, index));
  }

  /** 고른 시각(없으면 지금 시각)을 휠 가운데로 옮겨요. */
  private center(state: PickerState, behavior: ScrollBehavior, index = this.centerIndex(state)): void {
    const wheel = this.wheelOf(state);
    if (!wheel) return;
    const top = index * this.rowHeight(wheel);
    this.mark(wheel, index);
    if (behavior === "smooth" && typeof wheel.scrollTo === "function" && !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      wheel.scrollTo({ top, behavior });
    } else {
      wheel.scrollTop = top;
    }
  }

  // id 는 앱이 정한 상수라 따옴표가 들어가지 않아요.
  private element(state: PickerState): HTMLElement | null {
    return this.root.querySelector<HTMLElement>(`[data-time-picker="${state.options.id}"]`);
  }

  private stateOf(element: Element): PickerState | undefined {
    return this.states.get(element.closest<HTMLElement>("[data-time-picker]")?.dataset.timePicker ?? "");
  }

  private open(state: PickerState): void {
    // 두 칸이 나란히 있어도 펼친 판은 하나예요.
    for (const other of this.states.values()) {
      if (other !== state && other.open) {
        this.settleNow(other);
        other.open = false;
        this.update(other, null);
      }
    }
    state.open = true;
    state.entering = true;
    this.update(state, "[data-tp-wheel]");
    this.element(state)?.querySelector(".time-panel")?.scrollIntoView?.({ block: "nearest", behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  private close(state: PickerState): void {
    this.settleNow(state);
    state.open = false;
    this.update(state, '[data-tp="toggle"]');
  }

  /** 그 시의 정시를 값으로 해요. 바뀌었을 때만 change 를 올려요. */
  private commit(state: PickerState, hour: number): void {
    const value = hourClock(hour);
    if (value === state.value) return;
    state.value = value;
    this.paint(state);
    this.element(state)?.querySelector("input[type=hidden]")?.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /** 한 줄을 골랐어요: 값으로 하고 다음으로 넘어가요. */
  private choose(state: PickerState, hour: number): void {
    // 누른 줄이 답이에요. 앞서 넘기다 만 휠 자리가 접힐 때 이 값을 덮지 않게 해요.
    this.touched = null;
    this.commit(state, hour);
    const next = this.following(state);
    if (next) this.open(next);
    else this.close(state);
  }

  /** 방향키로 한 줄씩: 값을 바꾸고 그 줄을 가운데로 옮기되 칸은 열어 둬요. */
  private step(state: PickerState, index: number): void {
    const hours = this.hours(state);
    const clamped = Math.min(hours.length - 1, Math.max(0, index));
    // 휠이 부드럽게 넘어가는 중에 접혀도 방향키로 고른 값이 남아요.
    this.touched = null;
    this.commit(state, hours[clamped]!);
    this.center(state, "smooth", clamped);
  }

  /** 나란한 두 칸(.time-range)의 앞 칸이면 뒤 칸. 꺼진 칸이면 없어요. */
  private following(state: PickerState): PickerState | undefined {
    const element = this.element(state);
    const pickers = [...(element?.closest(".time-range")?.querySelectorAll("[data-time-picker]") ?? [])];
    const next = pickers[pickers.indexOf(element!) + 1];
    const nextState = next ? this.stateOf(next) : undefined;
    return nextState && !nextState.options.disabled ? nextState : undefined;
  }

  private onClick(event: Event): void {
    const target = event.target as HTMLElement;
    const option = target.closest<HTMLElement>("[data-time-picker] [data-tp-hour]");
    const optionState = option && this.stateOf(option);
    if (option && optionState?.open) {
      this.choose(optionState, Number(option.dataset.tpHour));
      return;
    }
    const button = target.closest<HTMLButtonElement>("[data-time-picker] [data-tp='toggle']");
    const state = button && this.stateOf(button);
    if (!button || !state) return;
    if (state.open) this.close(state);
    else this.open(state);
  }

  private onPointerDown(event: Event): void {
    const wheel = (event.target as HTMLElement).closest?.<HTMLElement>("[data-tp-wheel]");
    if (wheel) this.touched = wheel;
  }

  private onScroll(event: Event): void {
    const wheel = event.target as HTMLElement;
    if (!wheel.matches?.("[data-tp-wheel]")) return;
    const state = this.stateOf(wheel);
    if (!state?.open) return;
    this.mark(wheel, this.centeredIndex(state, wheel));
    clearTimeout(this.settleTimer);
    this.settleTimer = setTimeout(() => this.settle(wheel), SETTLE_MS);
  }

  private onScrollEnd(event: Event): void {
    const wheel = event.target as HTMLElement;
    if (!wheel.matches?.("[data-tp-wheel]")) return;
    clearTimeout(this.settleTimer);
    this.settle(wheel);
  }

  /** 휠이 멈추기를 기다리지 않고 지금 가운데 줄로 정해요. 넘기자마자 칸을 접어도 본 시각이 남아요. */
  private settleNow(state: PickerState): void {
    const wheel = this.wheelOf(state);
    if (!wheel) return;
    clearTimeout(this.settleTimer);
    this.settle(wheel);
  }

  /** 넘기던 휠이 멈췄어요. 손으로 넘긴 휠이면 가운데 줄의 정시가 값이에요. */
  private settle(wheel: HTMLElement): void {
    const state = this.stateOf(wheel);
    if (!state?.open || !wheel.isConnected || wheel !== this.touched) return;
    const hour = this.hours(state)[this.centeredIndex(state, wheel)]!;
    // 같은 시에 멈췄으면 그대로 둬요. 예전에 분까지 저장한 값(05:57)을 손대지 않고 지나가도 바뀌지 않아요.
    if (hour !== this.hourOf(state)) this.commit(state, hour);
  }

  private onKeyDown(event: KeyboardEvent): void {
    const target = event.target as HTMLElement;
    const state = target.closest?.("[data-time-picker]") ? this.stateOf(target) : undefined;
    if (!state?.open) return;
    if (event.key === "Escape") {
      event.preventDefault();
      this.close(state);
      return;
    }
    if (!target.matches?.("[data-tp-wheel]")) return;
    // 휠이 부드럽게 넘어가는 중일 수 있어 스크롤 자리가 아니라 고른 시각에서 세요.
    const index = this.centerIndex(state);
    const last = this.hours(state).length - 1;
    const moves: Record<string, number> = { ArrowDown: index + 1, ArrowUp: index - 1, PageDown: index + 3, PageUp: index - 3, Home: 0, End: last };
    if (event.key in moves) {
      event.preventDefault();
      this.step(state, moves[event.key]!);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      this.choose(state, this.hours(state)[index]!);
    }
  }
}
