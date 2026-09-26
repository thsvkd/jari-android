import type { Page } from "@playwright/test";

import { expect } from "./fixtures";

export interface LayoutRules {
  /** 손가락으로 누르는 요소의 최소 크기(px). Material 3 는 48dp 를 권해요. */
  minTarget?: number;
  /** 규칙에서 뺄 요소(선택자). 이유를 주석으로 남기고 써요. */
  ignore?: string[];
}

// 48px 를 지킬 수 없는 자리와 그 이유. 여기 없는 요소는 모두 48px 이에요.
const SMALLER_TARGETS: Record<string, number> = {
  // 7칸 달력은 360dp 폰의 카드 안에서 한 칸 48px 폭을 낼 수 없어요. 높이는 48px, 폭은 WCAG 2.5.5(AAA)·Apple HIG 의 44px 를 지켜요.
  "[data-dp-day]": 44,
};

/**
 * 지금 화면에서 사용자가 겪을 레이아웃 결함을 모아요. 브라우저가 실제로 그린 상자를 재요.
 * 규칙을 바꿀 때는 왜 그 값인지 함께 적어요.
 */
export async function layoutProblems(page: Page, rules: LayoutRules = {}): Promise<string[]> {
  return page.evaluate(({ minTarget, ignore, smaller }) => {
    const problems: string[] = [];
    const ignored = (node: Element) => ignore.some((selector) => node.closest(selector));
    const name = (node: Element) => {
      const text = (node.textContent || node.getAttribute("aria-label") || "").trim().replace(/\s+/g, " ").slice(0, 24);
      const hook = ["data-action", "data-view", "name", "id", "class"].map((key) => node.getAttribute(key)).find(Boolean) ?? "";
      return `<${node.tagName.toLowerCase()} ${hook}> "${text}"`;
    };
    const visible = (node: Element) => {
      const box = node.getBoundingClientRect();
      const style = getComputedStyle(node);
      return box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none" && Number(style.opacity) > 0;
    };
    const nav = document.querySelector(".bottom-nav")?.getBoundingClientRect() ?? null;
    const header = document.querySelector(".topbar")?.getBoundingClientRect() ?? null;

    // 1. 가로로 밀리는 화면
    const root = document.scrollingElement!;
    if (root.scrollWidth > window.innerWidth + 1) problems.push(`가로 넘침: 문서 폭 ${root.scrollWidth}px > 화면 ${window.innerWidth}px`);

    // 2. 누르기 어려운 크기
    const interactive = [...document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, [role=button], summary")]
      .filter((node) => visible(node) && !ignored(node) && !(node as HTMLButtonElement).disabled);
    for (const node of interactive) {
      // 숨긴 입력은 감싼 label 이 누르는 자리예요.
      const target = node.matches("input[type=radio], input[type=checkbox]") ? node.closest("label") ?? node : node;
      const box = target.getBoundingClientRect();
      const floor = Object.entries(smaller).find(([selector]) => target.matches(selector))?.[1] ?? minTarget;
      if (Math.min(box.width, box.height) + 0.5 < floor) problems.push(`작은 터치 영역 ${Math.round(box.width)}×${Math.round(box.height)}: ${name(target)}`);
    }

    // 3. 잘린 글자 (말줄임표로 의도한 곳은 빼요)
    for (const node of document.querySelectorAll("main *, .bottom-nav *, .topbar *, .seat-dialog *, .action-sheet *, .toast")) {
      if (!visible(node) || ignored(node) || !node.childNodes.length) continue;
      const style = getComputedStyle(node);
      const clips = ["hidden", "clip"].includes(style.overflowX) || ["hidden", "clip"].includes(style.overflowY);
      if (!clips || style.textOverflow === "ellipsis") continue;
      const element = node as HTMLElement;
      if (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1) {
        const hasText = [...node.childNodes].some((child) => child.nodeType === Node.TEXT_NODE && child.textContent!.trim());
        if (hasText) problems.push(`잘린 글자: ${name(node)}`);
      }
    }

    // 3-1. 너무 작은 글자. 휴대폰에서 12px 아래는 읽기 어려워요.
    for (const node of document.querySelectorAll("body *")) {
      if (!visible(node) || ignored(node)) continue;
      const ownText = [...node.childNodes].some((child) => child.nodeType === Node.TEXT_NODE && child.textContent!.trim());
      const size = parseFloat(getComputedStyle(node).fontSize);
      if (ownText && size < 12) problems.push(`작은 글자 ${size}px: ${name(node)}`);
    }

    // 4. 떠 있는 요소끼리의 겹침: sticky 버튼·토스트가 하단 메뉴를 덮거나 가리면 안 돼요.
    if (nav) {
      for (const floating of document.querySelectorAll(".sticky-action, .toast:not(:empty)")) {
        if (!visible(floating) || ignored(floating)) continue;
        const box = floating.getBoundingClientRect();
        if (box.bottom > nav.top + 0.5 && box.top < nav.bottom) problems.push(`하단 메뉴와 겹침: ${name(floating)}`);
      }
    }
    const toast = document.querySelector(".toast:not(:empty)");
    const sticky = document.querySelector(".sticky-action");
    if (toast && sticky && visible(toast) && visible(sticky)) {
      const a = toast.getBoundingClientRect();
      const b = sticky.getBoundingClientRect();
      if (a.bottom > b.top && a.top < b.bottom) problems.push(`토스트가 버튼을 가림: ${name(sticky)}`);
    }

    // 5. 머리글 아래로 내용이 숨지 않아요.
    const first = document.querySelector("main > *");
    if (header && first && visible(first) && window.scrollY === 0 && first.getBoundingClientRect().top < header.bottom - 0.5) {
      problems.push("머리글이 화면 첫 요소를 가림");
    }

    // 5-1. 붙박이 버튼과 하단 메뉴 사이는 --cta-gap 이에요. 여기(스크롤 맨 위)서는 목록이 길면 버튼이 떠 있고,
    //      bottomClearance(맨 아래)에서는 제자리에 있어요. 두 자리 모두 같은 간격이어야 해요.
    const stickyButton = document.querySelector(".sticky-action");
    if (nav && stickyButton && visible(stickyButton)) {
      const want = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--cta-gap"));
      const gap = nav.top - stickyButton.getBoundingClientRect().bottom;
      if (gap >= 0 && Math.abs(gap - want) > 1) problems.push(`붙박이 버튼과 하단 메뉴 사이 ${Math.round(gap)}px (규칙 ${want}px, 떠 있을 때)`);
    }

    // 6. 화면 폭 버튼은 화면 좌우 여백선(--gutter)에 딱 맞아요. 홈·내 예약·즐겨찾기의 큰 버튼 폭이 제각각이던 문제를 잡아요.
    //    카드·시트·나란한 두 칸 안의 버튼과 글자 폭 버튼은 이 규칙 밖이에요.
    const screen = document.querySelector("main.screen");
    if (screen) {
      const box = screen.getBoundingClientRect();
      const style = getComputedStyle(screen);
      const left = box.left + parseFloat(style.paddingLeft);
      const right = box.right - parseFloat(style.paddingRight);
      for (const button of screen.querySelectorAll(".button")) {
        if (!visible(button) || ignored(button)) continue;
        if (button.closest(".card, .notice, .schedule-panel, .favourite-row, .two-columns, .invite-actions, .split-actions, .action-sheet-actions")) continue;
        const edge = button.getBoundingClientRect();
        if (edge.width < (right - left) * 0.6) continue;
        if (Math.abs(edge.left - left) > 1 || Math.abs(edge.right - right) > 1) {
          problems.push(`여백선에서 벗어난 화면 폭 버튼(왼쪽 ${Math.round(edge.left - left)}px, 오른쪽 ${Math.round(right - edge.right)}px): ${name(button)}`);
        }
      }
    }
    return problems;
  }, { minTarget: rules.minTarget ?? 48, ignore: rules.ignore ?? [], smaller: SMALLER_TARGETS });
}

/** 끝까지 내렸을 때 마지막 내용이 하단 메뉴 위로 올라와 있어야 해요. */
export async function bottomClearance(page: Page): Promise<string[]> {
  // 앱이 막 펼친 판을 부드럽게 스크롤해 보여 주는 중이면 내린 자리를 도로 끌어올려요(느린 실기기에서 드러남).
  // 그래서 맨 아래에 두 번 잇달아 멈춰 있는 것을 본 그 자리에서 곧바로 재요.
  const problem: string[] = await page.evaluate(async () => {
    const root = document.scrollingElement!;
    const pause = () => new Promise((resolve) => setTimeout(resolve, 120));
    let previous = -1;
    for (let attempt = 0; attempt < 30; attempt++) {
      window.scrollTo({ top: root.scrollHeight, behavior: "instant" });
      await pause();
      const atBottom = root.scrollTop + window.innerHeight >= root.scrollHeight - 1;
      if (atBottom && root.scrollTop === previous) break;
      previous = atBottom ? root.scrollTop : -1;
    }
    const problems: string[] = [];
    const nav = document.querySelector(".bottom-nav")?.getBoundingClientRect();
    // 붙박이 버튼(즐겨찾기 추가·다음: 조건 확인)과 하단 메뉴 사이는 떠 있든 제자리에 있든 --cta-gap 이에요.
    const sticky = document.querySelector(".sticky-action")?.getBoundingClientRect();
    if (nav && sticky && sticky.height > 0) {
      const want = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--cta-gap"));
      const gap = nav.top - sticky.bottom;
      if (Math.abs(gap - want) > 1) problems.push(`붙박이 버튼과 하단 메뉴 사이 ${Math.round(gap)}px (규칙 ${want}px)`);
    }
    const items = [...document.querySelectorAll("main *")].filter((node) => {
      const box = node.getBoundingClientRect();
      return box.height > 0 && getComputedStyle(node).position !== "sticky";
    });
    const last = Math.max(...items.map((node) => node.getBoundingClientRect().bottom));
    if (!nav || last <= nav.top + 0.5) return problems;
    // 무엇이 가리는지 적어 두면 다시 났을 때 바로 원인을 볼 수 있어요.
    const culprit = items.find((node) => node.getBoundingClientRect().bottom === last);
    const where = culprit ? `<${culprit.tagName.toLowerCase()} ${culprit.getAttribute("class") ?? ""}>` : "";
    problems.push(`끝까지 내려도 마지막 내용이 하단 메뉴에 ${Math.round(last - nav.top)}px 가려요 ${where} (scrollTop ${Math.round(root.scrollTop)}/${root.scrollHeight - window.innerHeight})`);
    return problems;
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  return problem;
}

/** 이 화면의 레이아웃 결함이 없어야 해요. 실패 메시지에 결함을 전부 적어요. */
export async function expectCleanLayout(page: Page, where: string, rules?: LayoutRules): Promise<void> {
  // 막 나타나는 요소(토스트는 14px 아래에서 올라와요)를 움직이는 중에 재면 느린 기기에서 겹친다고 잘못 봐요.
  // 끝나는 애니메이션만 기다려요. 스피너처럼 끝없이 도는 것은 빼요.
  await page.evaluate(() => Promise.race([
    Promise.all(document.getAnimations()
      .filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
      .map((animation) => animation.finished.catch(() => undefined))),
    new Promise((resolve) => setTimeout(resolve, 2000)),
  ]));
  const problems = [...(await layoutProblems(page, rules)), ...(await bottomClearance(page))];
  expect(problems, `${where} 레이아웃`).toEqual([]);
}
