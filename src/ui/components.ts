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

export type Tone = "accent" | "success" | "warning" | "danger" | "muted" | "solid";

export interface BadgeOptions {
  label: string;
  tone?: Tone;
  /** pill: 둥근 알약(상태), tag: 모서리가 덜 둥근 꼬리표(카드 머리·좌석). */
  shape?: "pill" | "tag";
  /** 글자 앞의 작은 점. */
  dot?: boolean;
  /** 한 줄을 통째로 차지해야 하면 "p"·"div". */
  element?: "span" | "p" | "div";
  className?: string;
}

/** 상태를 짧게 알리는 표시. 모양은 두 가지, 색은 tone 하나로만 정해요. */
export function badge(options: BadgeOptions): Html {
  const element = options.element ?? "span";
  const tone = options.tone ?? "accent";
  return `<${element} class="${cx("badge", options.shape ?? "pill", tone !== "accent" && tone, options.className)}">${options.dot ? '<i aria-hidden="true"></i>' : ""}${esc(options.label)}</${element}>`;
}

export interface NoticeOptions {
  /** 없으면 기본(파랑), calm 은 안심(초록), warning 은 주의(주황). */
  tone?: "calm" | "warning";
  title?: string;
  text: string;
  /** 방금 생긴 오류라 화면 낭독기가 바로 읽어야 하면. */
  alert?: boolean;
  className?: string;
}

/** 본문 사이의 안내 상자. 제목이 있으면 굵게 한 줄, 설명은 그 아래에 둬요. */
export function notice(options: NoticeOptions): Html {
  const body = options.title ? `<b>${esc(options.title)}</b><p>${esc(options.text)}</p>` : esc(options.text);
  return `<div ${attrs({ class: cx("notice", options.tone, options.className), role: options.alert ? "alert" : undefined })}>${body}</div>`;
}

const MARKS = {
  /** 열차 앞모습과 시계: 찾는 것·조회된 열차가 없을 때. */
  train: '<rect x="13" y="15" width="38" height="30" rx="9"/><path d="M20 45l-5 7m29-7 5 7M21 26h22M23 36h4m10 0h4"/><circle class="filled" cx="46" cy="17" r="7"/><path class="signal" d="M46 14v3l2 2"/>',
  /** 열차와 책갈피: 즐겨찾기. */
  favourite: '<path d="M15 43h34M19 43l-5 8m31-8 5 8"/><circle cx="17" cy="28" r="6"/><circle cx="47" cy="28" r="6"/><path d="M23 28h18M32 28v-7"/><path class="filled" d="M27 12h10v14l-5-3-5 3z"/>',
  /** 종: 알림. */
  bell: '<path d="M20 42V30a12 12 0 0 1 24 0v12l4 5H16z"/><path d="M28 52a4 4 0 0 0 8 0"/>',
  /** 느낌표: 열지 못함. */
  alert: '<circle cx="32" cy="32" r="20"/><path d="M32 22v12"/><circle class="filled" cx="32" cy="42" r="2.4"/>',
} as const;

export type MarkKind = keyof typeof MARKS;

/** 빈 화면·대기 카드의 둥근 그림. 글리프 대신 같은 굵기의 선 그림 하나로 맞춰요. */
export function emptyMark(kind: MarkKind): Html {
  return `<span class="empty-mark" aria-hidden="true"><svg viewBox="0 0 64 64" fill="none">${MARKS[kind]}</svg></span>`;
}

export interface EmptyStateOptions {
  mark?: MarkKind;
  title?: string;
  text: string;
  /** 다음에 할 일 버튼(button() 결과). */
  action?: Html;
  /** 목록 안의 작은 빈칸. 그림 없이 한 줄만. */
  compact?: boolean;
}

/** 보여줄 것이 없을 때: 그림, 무엇이 없는지, 어떻게 채우는지, 할 일. */
export function emptyState(options: EmptyStateOptions): Html {
  return `<div class="${cx("empty", options.compact && "compact")}">${options.mark ? emptyMark(options.mark) : ""}${options.title ? `<h2>${esc(options.title)}</h2>` : ""}<p>${esc(options.text)}</p>${options.action ?? ""}</div>`;
}

export interface ListRowOptions {
  title: string;
  hint?: string;
  /** 오른쪽 글자(escape 해요). */
  value?: string;
  /** value 대신 그대로 넣는 오른쪽 HTML(배지·스테퍼·스피너). */
  trailing?: Html;
  /** 둘 다 없으면 누를 수 없는 줄이에요. */
  action?: string;
  view?: string;
  disabled?: boolean;
}

/** 설정 목록의 한 줄. 누를 수 있으면 button, 아니면 div 라서 눌림 표시가 누를 수 있는 줄에만 생겨요. */
export function listRow(options: ListRowOptions): Html {
  const text = `<span><b>${esc(options.title)}</b>${options.hint ? `<small>${esc(options.hint)}</small>` : ""}</span>`;
  const end = options.trailing ?? (options.value ? `<em>${esc(options.value)}</em>` : "");
  if (!options.action && !options.view) return `<div class="settings-row static">${text}${end}</div>`;
  return `<button ${attrs({ type: "button", class: "settings-row", "data-action": options.action, "data-view": options.view, disabled: options.disabled })}>${text}${end}</button>`;
}

/** 화면 위에 뜨는 창(확인 시트·좌석표). 뒤로가기·Escape 로 닫는 것은 앱의 closeTopOverlay 가 맡아요. */
export function dialog(options: { className: string; labelledBy: string; content: Html }): Html {
  return `<div class="modal-backdrop" role="presentation"><section ${attrs({ class: options.className, role: "dialog", "aria-modal": "true", "aria-labelledby": options.labelledBy })}>${options.content}</section></div>`;
}
