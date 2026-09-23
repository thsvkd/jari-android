import { describe, expect, it } from "vitest";

import { button, linkButton } from "./components";
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
