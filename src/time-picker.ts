// 앱 테마를 따르는 시간 선택기. Android WebView 의 기본 시계는 앱 테마를 무시해서 직접 그려요.
// 검색 시간대는 대략 고르니 시·분 슬라이더 두 개로, 분은 5분 단위로 골라요. 위에는 고른 시각을 크게 보여요.
// 값은 숨은 입력에 HH:MM 으로 두고, 바뀔 때마다 change 이벤트를 올려 기존 폼 흐름을 그대로 써요.

import { attrs, cx, esc } from "./ui/html";

export const MINUTE_STEP = 5;

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

/** 7 → "오전 7시". 시 슬라이더가 이렇게 읽혀요. */
export function hourLabel(hour: number): string {
  return `${hour < 12 ? "오전" : "오후"} ${hour % 12 || 12}시`;
}

/** 시만 바꿔요. 분은 그대로 두고(5분 단위가 아니어도), 값이 없었으면 정각이에요. */
export function withHour(value: string, hour: number): string {
  return `${pad(hour)}:${isClock(value) ? pad(parts(value)[1]) : "00"}`;
}

/** 분만 바꿔요. 시를 아직 고르지 않았으면 바꿀 수 없어요. */
export function withMinute(value: string, minute: number): string | null {
  return isClock(value) ? `${pad(parts(value)[0])}:${pad(minute)}` : null;
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

type Part = "hour" | "minute";

interface SliderView {
  min: number;
  max: number;
  step: number;
  value: number;
  /** 슬라이더 옆 글자이자 aria-valuetext. */
  text: string;
  /** 채운 구간(--fill). */
  fill: string;
  disabled: boolean;
}

const CLOCK = '<svg class="date-field-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>';
const CHEVRON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
const PART_NAME: Record<Part, string> = { hour: "시", minute: "분" };

/**
 * 앱 루트 하나에 붙는 시간 선택기. 날짜 선택기(DatePicker)와 같은 방식으로 앱의 전체 다시 그리기를 견뎌요.
 * 앱은 render() 가 돌려준 HTML 을 화면에 넣고, 다시 그린 뒤 refresh() 를 불러요.
 *
 * 슬라이더를 손으로 끌다 떼면 곧장 다음 단계로 넘어가요: 시 → 분 → (나란한 두 칸의 앞 칸이면) 뒤 칸의 시 → 닫기.
 * 방향키나 보조 기기로 값을 바꿀 때는 넘어가지 않아요. 잘못 뗐으면 큰 시각의 시·분을 눌러 그 슬라이더로 돌아가요.
 */
export class TimePicker {
  private readonly states = new Map<string, PickerState>();
  // 손가락·마우스로 누른 슬라이더. 뗄 때 이 슬라이더였을 때만 넘어가요.
  private pressed: HTMLInputElement | null = null;

  constructor(private readonly root: HTMLElement) {
    this.onClick = this.onClick.bind(this);
    this.onKeyDown = this.onKeyDown.bind(this);
    this.onInput = this.onInput.bind(this);
    this.onPointerDown = this.onPointerDown.bind(this);
    this.onPointerUp = this.onPointerUp.bind(this);
    this.onPointerCancel = this.onPointerCancel.bind(this);
    root.addEventListener("click", this.onClick);
    root.addEventListener("keydown", this.onKeyDown);
    root.addEventListener("input", this.onInput);
    root.addEventListener("pointerdown", this.onPointerDown);
    // 손잡이를 끌다 루트 밖에서 떼도 알아야 해서 문서에서 들어요.
    root.ownerDocument.addEventListener("pointerup", this.onPointerUp);
    root.ownerDocument.addEventListener("pointercancel", this.onPointerCancel);
  }

  dispose(): void {
    this.root.removeEventListener("click", this.onClick);
    this.root.removeEventListener("keydown", this.onKeyDown);
    this.root.removeEventListener("input", this.onInput);
    this.root.removeEventListener("pointerdown", this.onPointerDown);
    this.root.ownerDocument.removeEventListener("pointerup", this.onPointerUp);
    this.root.ownerDocument.removeEventListener("pointercancel", this.onPointerCancel);
    this.states.clear();
    this.pressed = null;
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
      const value = element.querySelector<HTMLInputElement>("input[type=hidden]")?.value ?? "";
      if (!state || value === state.value || (value && !isClock(value))) continue;
      state.value = value;
      this.update(state, null);
    }
  }

  private sliders(state: PickerState): Record<Part, SliderView> {
    const [from, to] = state.options.hours ?? [5, 23];
    const [hour, minute] = state.value ? parts(state.value) : [null, null];
    const fill = (value: number, min: number, max: number) => `${Math.min(100, Math.max(0, ((value - min) / (max - min || 1)) * 100))}%`;
    const hourValue = hour ?? from;
    const minuteValue = minute ?? 0;
    return {
      hour: { min: from, max: to, step: 1, value: hourValue, text: hour === null ? "–" : hourLabel(hour), fill: fill(hourValue, from, to), disabled: false },
      // 분은 시를 고른 뒤에 움직여요. 5분 단위가 아닌 값(05:57)은 글자로는 그대로, 손잡이는 가까운 칸에 둬요.
      minute: { min: 0, max: 60 - MINUTE_STEP, step: MINUTE_STEP, value: minuteValue, text: minute === null ? "–" : `${pad(minute)}분`, fill: fill(minuteValue, 0, 60 - MINUTE_STEP), disabled: hour === null },
    };
  }

  private fieldText(state: PickerState): string {
    return state.value ? clockLabel(state.value) : "시각을 골라 주세요";
  }

  private fieldName(state: PickerState): string {
    return `${state.options.label} ${state.value ? clockLabel(state.value) : "선택 안 함"}`;
  }

  /** 큰 시각. 시·분이 각각 버튼이라 누르면 그 슬라이더로 돌아가요. */
  private big(state: PickerState): string {
    const { hour, minute } = this.sliders(state);
    const [shownHour, shownMinute] = state.value ? clockLabel(state.value).split(":") : ["–", "–"];
    return `<button ${attrs({ type: "button", "data-tp": "hour", "aria-label": `시 고르기, ${hour.text}` })}>${esc(shownHour!)}</button><span aria-hidden="true">:</span><button ${attrs({ type: "button", "data-tp": "minute", "aria-label": `분 고르기, ${minute.text}`, disabled: minute.disabled })}>${esc(shownMinute!)}</button>`;
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
    const views = this.sliders(state);
    const row = (part: Part) => {
      const view = views[part];
      return `<label class="time-slider"><span>${PART_NAME[part]}</span><b>${esc(view.text)}</b><input ${attrs({
        type: "range",
        "data-tp-slider": part,
        min: view.min,
        max: view.max,
        step: view.step,
        value: view.value,
        "aria-label": PART_NAME[part],
        "aria-valuetext": view.text,
        style: `--fill: ${view.fill}`,
        disabled: view.disabled,
      })}></label>`;
    };
    return `<div class="${cx("time-panel", state.entering && "entering")}" id="${esc(id)}-panel" role="group" aria-label="${esc(label)} 고르기">
      <p class="time-panel-cap">${esc(label)} 고르는 중</p>
      <p class="time-big">${this.big(state)}</p>
      ${row("hour")}${row("minute")}
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
    if (focus) replacement.querySelector<HTMLElement>(focus)?.focus({ preventScroll: true });
  }

  /** 끄는 동안에는 통째로 다시 그리지 않고 글자·값만 고쳐요. 바꿔 끼우면 손가락 밑의 손잡이가 사라져요. */
  private paint(state: PickerState): void {
    const element = this.element(state);
    if (!element) return;
    element.querySelector<HTMLInputElement>("input[type=hidden]")!.value = state.value;
    const toggle = element.querySelector<HTMLElement>("[data-tp='toggle']")!;
    toggle.setAttribute("aria-label", this.fieldName(state));
    toggle.querySelector("b")!.textContent = this.fieldText(state);
    const big = element.querySelector(".time-big");
    if (big) big.innerHTML = this.big(state);
    for (const [part, view] of Object.entries(this.sliders(state))) {
      const slider = element.querySelector<HTMLInputElement>(`[data-tp-slider="${part}"]`);
      if (!slider) continue;
      slider.disabled = view.disabled;
      slider.value = String(view.value);
      slider.setAttribute("aria-valuetext", view.text);
      slider.style.setProperty("--fill", view.fill);
      slider.previousElementSibling!.textContent = view.text;
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
        other.open = false;
        this.update(other, null);
      }
    }
    state.open = true;
    state.entering = true;
    this.update(state, '[data-tp-slider="hour"]');
    this.element(state)?.querySelector(".time-panel")?.scrollIntoView?.({ block: "nearest", behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }

  private close(state: PickerState): void {
    state.open = false;
    this.update(state, '[data-tp="toggle"]');
  }

  /** 슬라이더 값을 시각에 옮겨요. 바뀌었을 때만 change 를 올려요. */
  private commit(slider: HTMLInputElement): PickerState | undefined {
    const state = this.stateOf(slider);
    if (!state) return undefined;
    const value = slider.dataset.tpSlider === "hour" ? withHour(state.value, Number(slider.value)) : withMinute(state.value, Number(slider.value));
    if (value && value !== state.value) {
      state.value = value;
      this.paint(state);
      this.element(state)?.querySelector("input[type=hidden]")?.dispatchEvent(new Event("change", { bubbles: true }));
    }
    return state;
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
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-time-picker] button");
    const state = button && this.stateOf(button);
    if (!button || !state) return;
    const { tp } = button.dataset;
    if (tp === "toggle") {
      if (state.open) this.close(state);
      else this.open(state);
    } else if (tp === "hour" || tp === "minute") {
      this.element(state)?.querySelector<HTMLElement>(`[data-tp-slider="${tp}"]`)?.focus();
    }
  }

  private onInput(event: Event): void {
    const slider = event.target as HTMLInputElement;
    if (slider.dataset?.tpSlider) this.commit(slider);
  }

  private onPointerDown(event: Event): void {
    const slider = (event.target as HTMLElement).closest<HTMLInputElement>("[data-tp-slider]");
    this.pressed = slider && !((event as PointerEvent).button > 0) ? slider : null;
  }

  private onPointerCancel(): void {
    // 세로로 쓸어 화면이 스크롤됐어요. 고른 게 아니라 넘어가지 않아요.
    this.pressed = null;
  }

  // 손을 떼면 곧장 다음으로: 시 → 분 → (앞 칸이면) 뒤 칸의 시 → 닫기.
  private onPointerUp(): void {
    const slider = this.pressed;
    this.pressed = null;
    if (!slider?.isConnected || slider.disabled) return;
    // 누른 자리가 원래 값이면 input 이 오지 않아요. 비어 있던 칸도 뗀 자리로 정해요.
    const state = this.commit(slider);
    if (!state?.open) return;
    if (slider.dataset.tpSlider === "hour") {
      this.element(state)?.querySelector<HTMLElement>('[data-tp-slider="minute"]')?.focus({ preventScroll: true });
      return;
    }
    const next = this.following(state);
    if (next) this.open(next);
    else this.close(state);
  }

  private onKeyDown(event: KeyboardEvent): void {
    const target = event.target as HTMLElement;
    const state = target.closest?.("[data-time-picker]") ? this.stateOf(target) : undefined;
    if (!state || event.key !== "Escape" || !state.open) return;
    event.preventDefault();
    this.close(state);
  }
}
