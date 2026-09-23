// 앱 테마를 따르는 시간 선택기. Android WebView 의 기본 시계는 앱 테마를 무시해서 직접 그려요.
// 검색 시간대는 대략 고르니 시는 칩으로, 분은 10분 단위로 빠르게 골라요.
// 값은 숨은 입력에 HH:MM 으로 두고, 고를 때마다 change 이벤트를 올려 기존 폼 흐름을 그대로 써요.

import { chip } from "./ui/components";
import { attrs, cx, esc } from "./ui/html";

export const MINUTES = [0, 10, 20, 30, 40, 50];

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

/** 7 → "오전 7시". 칩의 이름으로 읽혀요. */
export function hourLabel(hour: number): string {
  return `${hour < 12 ? "오전" : "오후"} ${hour % 12 || 12}시`;
}

/** 시만 바꿔요. 분은 그대로 두고(10분 단위가 아니어도), 값이 없었으면 정각이에요. */
export function withHour(value: string, hour: number): string {
  return `${pad(hour)}:${isClock(value) ? pad(parts(value)[1]) : "00"}`;
}

/** 분만 바꿔요. 시를 아직 고르지 않았으면 바꿀 수 없어요. */
export function withMinute(value: string, minute: number): string | null {
  return isClock(value) ? `${pad(parts(value)[0])}:${pad(minute)}` : null;
}

export interface HourGroup {
  label: "오전" | "오후";
  /** 4칸씩 끊은 줄. 모자란 칸은 null 이라 방향키가 세로로 같은 칸을 따라가요. */
  rows: Array<Array<number | null>>;
}

/** 고를 수 있는 시를 오전·오후로 나눠 4칸씩 줄지어요. 360dp 폰에서도 칩 폭이 48px 을 넘어요. */
export function hourGroups(from: number, to: number): HourGroup[] {
  const groups: HourGroup[] = [];
  for (const [label, first, last] of [["오전", from, Math.min(to, 11)], ["오후", Math.max(from, 12), to]] as const) {
    const hours = Array.from({ length: Math.max(0, last - first + 1) }, (_, index) => first + index);
    if (!hours.length) continue;
    const rows = Array.from({ length: Math.ceil(hours.length / 4) }, (_, row) =>
      Array.from({ length: 4 }, (_, column) => hours[row * 4 + column] ?? null));
    groups.push({ label, rows });
  }
  return groups;
}

/**
 * 방향키가 옮길 칩. 좌우·Home/End 는 차례대로, 위아래는 같은 칸을 따라(오전·오후를 건너) 움직여요.
 * 끝에서는 제자리에 머물고, 다루지 않는 키면 null 이에요.
 */
export function moveInRows(rows: Array<Array<number | null>>, current: number, key: string): number | null {
  const flat = rows.flat().filter((value): value is number => value !== null);
  const index = flat.indexOf(current);
  if (index < 0) return null;
  switch (key) {
    case "ArrowLeft": return flat[Math.max(0, index - 1)]!;
    case "ArrowRight": return flat[Math.min(flat.length - 1, index + 1)]!;
    case "Home": return flat[0]!;
    case "End": return flat[flat.length - 1]!;
    case "ArrowUp":
    case "ArrowDown": {
      const step = key === "ArrowUp" ? -1 : 1;
      const row = rows.findIndex((cells) => cells.includes(current));
      const column = rows[row]!.indexOf(current);
      for (let next = row + step; next >= 0 && next < rows.length; next += step) {
        const value = rows[next]![column];
        if (value !== null && value !== undefined) return value;
      }
      return current;
    }
    default: return null;
  }
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
 */
export class TimePicker {
  private readonly states = new Map<string, PickerState>();

  constructor(private readonly root: HTMLElement) {
    this.onClick = this.onClick.bind(this);
    this.onKeyDown = this.onKeyDown.bind(this);
    root.addEventListener("click", this.onClick);
    root.addEventListener("keydown", this.onKeyDown);
  }

  dispose(): void {
    this.root.removeEventListener("click", this.onClick);
    this.root.removeEventListener("keydown", this.onKeyDown);
    this.states.clear();
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

  /** 앱이 화면을 다시 그린 뒤: 숨은 입력에 되살려 둔 값이 다르면 받아들여요. */
  refresh(): void {
    for (const element of this.root.querySelectorAll<HTMLElement>("[data-time-picker]")) {
      const state = this.states.get(element.dataset.timePicker ?? "");
      const value = element.querySelector("input")?.value ?? "";
      if (!state || value === state.value || (value && !isClock(value))) continue;
      state.value = value;
      this.update(state, null);
    }
  }

  private hours(state: PickerState): [number, number] {
    return state.options.hours ?? [5, 23];
  }

  private markup(state: PickerState): string {
    const { id, name, label, disabled } = state.options;
    return `<div class="${cx("time-picker", state.open && "open")}" data-time-picker="${esc(id)}">
      <input ${attrs({ type: "hidden", id, name, value: state.value, disabled })}>
      <button ${attrs({
        type: "button",
        class: "date-field time-field",
        "data-tp": "toggle",
        "aria-expanded": state.open,
        "aria-controls": state.open ? `${id}-panel` : null,
        "aria-label": `${label} ${state.value ? clockLabel(state.value) : "선택 안 함"}`,
        disabled,
      })}>${CLOCK}<b>${state.value ? clockLabel(state.value) : "시각을 골라 주세요"}</b>${CHEVRON}</button>
      ${state.open ? this.panel(state) : ""}
    </div>`;
  }

  private panel(state: PickerState): string {
    const { id, label } = state.options;
    const [hour, minute] = state.value ? parts(state.value) : [null, null];
    const groups = hourGroups(...this.hours(state));
    const hours = groups.flatMap((group) => group.rows.flat()).filter((value): value is number => value !== null);
    // 방향키로 들어올 한 칸: 고른 시·분, 없으면 첫 칩이에요.
    const hourStop = hour !== null && hours.includes(hour) ? hour : hours[0];
    const minuteStop = minute !== null && MINUTES.includes(minute) ? minute : MINUTES[0];
    const hourChips = (group: HourGroup) => group.rows.flat().filter((value): value is number => value !== null)
      .map((value) => chip({
        label: `${value % 12 || 12}시`,
        pressed: value === hour,
        attrs: { "data-tp-hour": value, "aria-label": hourLabel(value), tabindex: value === hourStop ? 0 : -1 },
      })).join("");
    const minuteChips = MINUTES.map((value) => chip({
      label: `${pad(value)}분`,
      pressed: value === minute,
      disabled: hour === null,
      attrs: { "data-tp-minute": value, tabindex: value === minuteStop ? 0 : -1 },
    })).join("");
    return `<div class="${cx("time-panel", state.entering && "entering")}" id="${esc(id)}-panel" role="group" aria-label="${esc(label)} 고르기">
      ${groups.map((group) => `<p id="${esc(id)}-${group.label === "오전" ? "am" : "pm"}">${group.label}</p><div class="time-hours" role="group" aria-labelledby="${esc(id)}-${group.label === "오전" ? "am" : "pm"}">${hourChips(group)}</div>`).join("")}
      <p id="${esc(id)}-min">분</p><div class="time-minutes" role="group" aria-labelledby="${esc(id)}-min">${minuteChips}</div>
    </div>`;
  }

  /** 이 선택기 하나만 다시 그리고, focus 선택자가 있으면 그 칩에 초점을 둬요. */
  private update(state: PickerState, focus: string | null): void {
    const element = this.element(state);
    if (!element) return;
    const template = document.createElement("template");
    template.innerHTML = this.markup(state);
    const replacement = template.content.firstElementChild as HTMLElement;
    element.replaceWith(replacement);
    state.entering = false;
    if (focus) replacement.querySelector<HTMLElement>(focus)?.focus();
  }

  // id 는 앱이 정한 상수라 따옴표가 들어가지 않아요.
  private element(state: PickerState): HTMLElement | null {
    return this.root.querySelector<HTMLElement>(`[data-time-picker="${state.options.id}"]`);
  }

  private stateOf(element: Element): PickerState | undefined {
    return this.states.get(element.closest<HTMLElement>("[data-time-picker]")?.dataset.timePicker ?? "");
  }

  private pick(state: PickerState, value: string, focus: string): void {
    state.value = value;
    this.update(state, focus);
    this.element(state)?.querySelector("input")?.dispatchEvent(new Event("change", { bubbles: true }));
  }

  private onClick(event: Event): void {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-time-picker] button");
    const state = button && this.stateOf(button);
    if (!button || !state) return;
    const { tpHour, tpMinute, tp } = button.dataset;
    if (tpHour) {
      // 시를 고르면 펼친 채로 두어 분을 이어서 골라요.
      this.pick(state, withHour(state.value, Number(tpHour)), `[data-tp-hour="${Number(tpHour)}"]`);
    } else if (tpMinute) {
      const value = withMinute(state.value, Number(tpMinute));
      if (!value) return;
      state.open = false;
      this.pick(state, value, '[data-tp="toggle"]');
    } else if (tp === "toggle") {
      state.open = !state.open;
      state.entering = state.open;
      // 두 칸이 나란히 있어도 펼친 판은 하나예요.
      if (state.open) {
        for (const other of this.states.values()) {
          if (other !== state && other.open) {
            other.open = false;
            this.update(other, null);
          }
        }
      }
      this.update(state, '[data-tp="toggle"]');
      this.element(state)?.querySelector(".time-panel")?.scrollIntoView?.({ block: "nearest", behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  }

  private onKeyDown(event: KeyboardEvent): void {
    const target = event.target as HTMLElement;
    const state = target.closest?.("[data-time-picker]") ? this.stateOf(target) : undefined;
    if (!state) return;
    if (event.key === "Escape" && state.open) {
      event.preventDefault();
      state.open = false;
      this.update(state, '[data-tp="toggle"]');
      return;
    }
    const { tpHour, tpMinute } = target.dataset;
    const attribute = tpHour ? "data-tp-hour" : tpMinute ? "data-tp-minute" : null;
    if (!attribute) return;
    const rows = tpHour ? hourGroups(...this.hours(state)).flatMap((group) => group.rows) : [MINUTES];
    const next = moveInRows(rows, Number(tpHour ?? tpMinute), event.key);
    if (next === null) return;
    event.preventDefault();
    // 고르지는 않고 초점만 옮겨요. 시·분 무리마다 Tab 이 멈추는 칩은 하나예요.
    const chips = [...target.closest(".time-panel")!.querySelectorAll<HTMLButtonElement>(`[${attribute}]`)];
    const nextChip = chips.find((chip) => chip.getAttribute(attribute) === String(next));
    if (!nextChip) return;
    for (const chip of chips) chip.tabIndex = chip === nextChip ? 0 : -1;
    nextChip.focus();
  }
}
