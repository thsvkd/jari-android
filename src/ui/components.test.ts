import { describe, expect, it } from "vitest";

import { badge, button, dialog, emptyState, linkButton, listRow, notice } from "./components";
import { attrs, cx, esc } from "./html";

describe("html", () => {
  it("escapes text so user data never becomes markup", () => {
    expect(esc(`<img src=x onerror="a">&'`)).toBe("&lt;img src=x onerror=&quot;a&quot;&gt;&amp;&#39;");
    expect(esc(null)).toBe("");
  });

  it("writes aria/data booleans as words and drops other false flags", () => {
    expect(attrs({ "aria-pressed": false, "data-open": true, disabled: false, required: true, title: undefined, id: "a\"b" }))
      .toBe('aria-pressed="false" data-open="true" required id="a&quot;b"');
  });

  it("joins class names without empty gaps", () => {
    expect(cx("button", false, "", undefined, "primary")).toBe("button primary");
  });
});

describe("button", () => {
  it("keeps the class names and hooks the app and tests select by", () => {
    expect(button({ variant: "danger", action: "cancel-search", label: "그만 찾기" }))
      .toBe('<button type="button" class="button danger" data-action="cancel-search">그만 찾기</button>');
    expect(button({ variant: "ghost-danger", view: "activity", label: "x" })).toContain('class="button ghost danger" data-view="activity"');
    expect(button({ variant: "seat", className: "selected", label: "x" })).toContain('class="button seat-action selected"');
  });

  it("escapes labels and data, but not trusted content", () => {
    const html = button({ variant: "text", label: "<b>", attrs: { "data-edit-favourite": '"x' } });
    expect(html).toContain("&lt;b&gt;");
    expect(html).toContain('data-edit-favourite="&quot;x"');
    expect(button({ content: "<span>a</span>" })).toContain("><span>a</span></button>");
  });

  it("never submits a form unless asked, and marks disabled buttons", () => {
    expect(button({ label: "a" })).toContain('type="button"');
    expect(button({ type: "submit", label: "a", disabled: true })).toMatch(/type="submit".* disabled>/);
    expect(button({ label: "새 즐겨찾기 추가", trailing: "＋" })).toContain(">새 즐겨찾기 추가 <span>＋</span></button>");
  });

  it("only links out to web addresses", () => {
    expect(linkButton({ label: "결제", href: "https://www.letskorail.com/" })).toContain('href="https://www.letskorail.com/"');
    expect(linkButton({ label: "결제", href: "javascript:alert(1)" })).toContain('href="#"');
  });
});

describe("stepper", () => {
  it("names both sides for screen readers and cannot be pressed past its ends", async () => {
    const { stepper } = await import("./components");
    const html = stepper({ label: "인원", value: "1명", decrease: "passenger-minus", increase: "passenger-plus", atMin: true, atMax: false });
    expect(html).toContain('data-action="passenger-minus" aria-label="인원 줄이기" disabled>−');
    expect(html).toContain('data-action="passenger-plus" aria-label="인원 늘리기">＋');
    expect(html).toContain('<output aria-live="polite">1명</output>');
  });
});

describe("badge · notice · emptyState · listRow · dialog", () => {
  it("gives every badge one base look and only a tone on top", () => {
    expect(badge({ label: "<통합>", tone: "success", className: "integrated-badge" }))
      .toBe('<span class="badge pill success integrated-badge">&lt;통합&gt;</span>');
    expect(badge({ label: "찾는 중", dot: true, element: "p", shape: "tag" }))
      .toBe('<p class="badge tag"><i aria-hidden="true"></i>찾는 중</p>');
  });

  it("escapes notice text and announces only alerts", () => {
    expect(notice({ tone: "warning", text: "<x>", alert: true })).toBe('<div class="notice warning" role="alert">&lt;x&gt;</div>');
    expect(notice({ title: "제목", text: "설명" })).toBe('<div class="notice"><b>제목</b><p>설명</p></div>');
  });

  it("draws the same line mark for every empty screen and keeps the action", () => {
    const html = emptyState({ mark: "bell", title: "새 알림이 없어요", text: "a", action: "<button>b</button>" });
    expect(html).toMatch(/^<div class="empty"><span class="empty-mark" aria-hidden="true"><svg/);
    expect(html).toContain("<h2>새 알림이 없어요</h2><p>a</p><button>b</button></div>");
    expect(emptyState({ compact: true, text: "없어요" })).toBe('<div class="empty compact"><p>없어요</p></div>');
  });

  it("makes a row a button only when it does something", () => {
    expect(listRow({ title: "앱 버전", value: "v1" })).toBe('<div class="settings-row static"><span><b>앱 버전</b></span><em>v1</em></div>');
    expect(listRow({ title: "휴대폰 알림", action: "request-push", disabled: true, value: "이용 불가" }))
      .toBe('<button type="button" class="settings-row" data-action="request-push" disabled><span><b>휴대폰 알림</b></span><em>이용 불가</em></button>');
  });

  it("wraps overlays in a labelled modal dialog", () => {
    expect(dialog({ className: "action-sheet", labelledBy: "t", content: "<h2 id=t>a</h2>" }))
      .toBe('<div class="modal-backdrop" role="presentation"><section class="action-sheet" role="dialog" aria-modal="true" aria-labelledby="t"><h2 id=t>a</h2></section></div>');
  });
});
