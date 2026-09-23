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
    return problems;
  }, { minTarget: rules.minTarget ?? 48, ignore: rules.ignore ?? [], smaller: SMALLER_TARGETS });
}

/** 끝까지 내렸을 때 마지막 내용이 하단 메뉴 위로 올라와 있어야 해요. */
export async function bottomClearance(page: Page): Promise<string[]> {
  await page.evaluate(() => window.scrollTo(0, document.scrollingElement!.scrollHeight));
  await page.waitForTimeout(150);
  const problem = await page.evaluate(() => {
    const nav = document.querySelector(".bottom-nav")?.getBoundingClientRect();
    const items = [...document.querySelectorAll("main *")].filter((node) => {
      const box = node.getBoundingClientRect();
      return box.height > 0 && getComputedStyle(node).position !== "sticky";
    });
    const last = Math.max(...items.map((node) => node.getBoundingClientRect().bottom));
    return nav && last > nav.top + 0.5 ? `끝까지 내려도 마지막 내용이 하단 메뉴에 ${Math.round(last - nav.top)}px 가려요` : null;
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  return problem ? [problem] : [];
}

/** 이 화면의 레이아웃 결함이 없어야 해요. 실패 메시지에 결함을 전부 적어요. */
export async function expectCleanLayout(page: Page, where: string, rules?: LayoutRules): Promise<void> {
  const problems = [...(await layoutProblems(page, rules)), ...(await bottomClearance(page))];
  expect(problems, `${where} 레이아웃`).toEqual([]);
}
