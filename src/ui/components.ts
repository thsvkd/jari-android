// 화면 곳곳에 같은 모양으로 나와야 하는 요소들. 글자는 escape 하고, Html 슬롯만 그대로 넣어요.
// 클래스 이름과 data-* 훅은 예전 마크업과 같아요. 앱의 onClick·onChange 와 테스트가 그 이름으로 찾아요.

import { attrs, cx, esc, type Attrs, type Html } from "./html";

export type ButtonVariant =
  | "primary"
  | "secondary"
  | "ghost"
  | "danger"
  | "ghost-danger"
  | "text"
  | "text-danger"
  | "icon"
  | "icon-danger"
  /** 열차 카드 안의 좌석 버튼(연한 파랑). */
  | "seat";

const BUTTON_CLASS: Record<ButtonVariant, string> = {
  primary: "button primary",
  secondary: "button secondary",
  ghost: "button ghost",
  danger: "button danger",
  "ghost-danger": "button ghost danger",
  text: "text-button",
  "text-danger": "text-button danger",
  icon: "icon-button",
  "icon-danger": "icon-button danger",
  seat: "button seat-action",
};

export interface ButtonOptions {
  /** 버튼 글자. escape 해요. */
  label?: string;
  /** 글자 뒤에 붙는 표시(→, ＋, ↗). escape 해서 <span> 에 넣어요. */
  trailing?: string;
  /** label 대신 그대로 넣는 안쪽 HTML(아이콘·여러 줄). */
  content?: Html;
  variant?: ButtonVariant;
  /** 홈의 큰 버튼. */
  roomy?: boolean;
  action?: string;
  view?: string;
  /** 폼을 제출하는 버튼만 "submit". 그 밖에는 폼 안에서도 제출하지 않아요. */
  type?: "button" | "submit";
  disabled?: boolean;
  ariaLabel?: string;
  title?: string;
  /** 자리 잡는 데만 쓰는 이름(sticky-action, favourite-add, seat-action…). */
  className?: string;
  /** 그 밖의 data-*·aria-* 속성. */
  attrs?: Attrs;
}

export function button(options: ButtonOptions): Html {
  const inner = options.content ?? `${esc(options.label)}${options.trailing ? ` <span>${esc(options.trailing)}</span>` : ""}`;
  return `<button ${attrs({
    type: options.type ?? "button",
    class: cx(BUTTON_CLASS[options.variant ?? "primary"], options.roomy && "roomy", options.className),
    "data-action": options.action,
    "data-view": options.view,
    "aria-label": options.ariaLabel,
    title: options.title,
    disabled: options.disabled,
    ...options.attrs,
  })}>${inner}</button>`;
}

/** 결제처럼 앱 밖으로 나가는 버튼 모양의 링크. http(s) 주소만 받아요. */
export function linkButton(options: { label: string; trailing?: string; href: string; variant?: ButtonVariant }): Html {
  const href = /^https?:\/\//i.test(options.href) ? options.href : "#";
  return `<a class="${BUTTON_CLASS[options.variant ?? "primary"]}" href="${esc(href)}" target="_blank" rel="noreferrer">${esc(options.label)}${options.trailing ? ` <span>${esc(options.trailing)}</span>` : ""}</a>`;
}

export interface ChoiceOptions {
  type: "radio" | "checkbox";
  name: string;
  value?: string;
  title: string;
  hint?: string;
  checked: boolean;
  /** tile: 두 칸 격자의 카드(열차 종류·좌석 등급), row: 설명이 긴 한 줄(좌석 등급 방식·마지막 열차까지). */
  look: "tile" | "row";
  className?: string;
}

/** 라디오·체크박스. 입력은 label 안에 있어 글자 어디를 눌러도 골라져요. */
export function choice(options: ChoiceOptions): Html {
  const input = `<input ${attrs({ type: options.type, name: options.name, value: options.value, checked: options.checked })}>`;
  const text = `<span><b>${esc(options.title)}</b>${options.hint ? `<small>${esc(options.hint)}</small>` : ""}</span>`;
  const base = options.look === "tile" ? "choice-card" : "check-row";
  return `<label class="${cx(base, options.className)}">${input}${text}</label>`;
}

/** 눌러서 켜고 끄는 작은 칩(좌석 열 고르기, 오늘·내일). */
export function chip(options: { label: string; pressed: boolean; disabled?: boolean; attrs?: Attrs }): Html {
  return `<button ${attrs({
    type: "button",
    class: cx("seat-chip", options.pressed && "selected"),
    ...options.attrs,
    "aria-pressed": options.pressed,
    disabled: options.disabled,
  })}>${esc(options.label)}</button>`;
}

export interface StepperOptions {
  /** 무엇을 세는지("인원", "찾기 상황 알림"). 화면 낭독기가 "인원 줄이기"처럼 읽어요. */
  label: string;
  /** 가운데 보이는 값(escape 해요). */
  value: string;
  decrease: string;
  increase: string;
  atMin: boolean;
  atMax: boolean;
  disabled?: boolean;
  small?: boolean;
}

/** −/＋ 로 값을 한 칸씩 옮겨요. 끝에 닿은 쪽은 누를 수 없게 해 눌러도 아무 일이 없는 버튼을 남기지 않아요. */
export function stepper(options: StepperOptions): Html {
  const side = (action: string, mark: string, verb: string, atEdge: boolean) =>
    `<button ${attrs({ type: "button", "data-action": action, "aria-label": `${options.label} ${verb}`, disabled: options.disabled || atEdge })}>${mark}</button>`;
  return `<div class="${cx("stepper", options.small && "small")}">${side(options.decrease, "−", "줄이기", options.atMin)}<output aria-live="polite">${esc(options.value)}</output>${side(options.increase, "＋", "늘리기", options.atMax)}</div>`;
}
