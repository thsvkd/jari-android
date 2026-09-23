import { afterEach, describe, expect, it, vi } from "vitest";

import { DatePicker, addMonths, departureRange, fullDateLabel, isIsoDate, monthGrid, moveFocus } from "./date-picker";

describe("날짜 계산", () => {
  it("lays a month out from Sunday in six fixed weeks", () => {
    const grid = monthGrid("2026-09"); // 2026년 9월 1일은 화요일
    expect(grid).toHaveLength(42);
    expect(grid.slice(0, 3)).toEqual([null, null, "2026-09-01"]);
    expect(grid.filter(Boolean)).toHaveLength(30);
    expect(grid[31]).toBe("2026-09-30");
    expect(grid.slice(32).every((day) => day === null)).toBe(true);
    expect(monthGrid("2028-02").filter(Boolean)).toHaveLength(29);
  });

  it("allows today through a year ahead, like the server's validate_date", () => {
    expect(departureRange(new Date(2026, 8, 23, 23, 59))).toEqual({ min: "2026-09-23", max: "2027-09-23" });
  });

  it("keeps the day inside the target month when paging months", () => {
    expect(addMonths("2026-01-31", 1)).toBe("2026-02-28");
    expect(addMonths("2026-12-15", 1)).toBe("2027-01-15");
    expect(addMonths("2026-03-31", -1)).toBe("2026-02-28");
  });

  it("moves focus with arrows, Home/End and PageUp/PageDown, never past the range", () => {
    const [min, max] = ["2026-09-23", "2027-09-23"];
    expect(moveFocus("2026-09-30", "ArrowRight", min, max)).toBe("2026-10-01");
    expect(moveFocus("2026-09-30", "ArrowDown", min, max)).toBe("2026-10-07");
    expect(moveFocus("2026-09-30", "ArrowUp", min, max)).toBe("2026-09-23");
    expect(moveFocus("2026-09-24", "ArrowLeft", min, max)).toBe("2026-09-23");
    expect(moveFocus("2026-09-23", "ArrowLeft", min, max)).toBe("2026-09-23");
    expect(moveFocus("2026-09-30", "Home", min, max)).toBe("2026-09-27");
    expect(moveFocus("2026-09-30", "End", min, max)).toBe("2026-10-03");
    expect(moveFocus("2026-10-31", "PageDown", min, max)).toBe("2026-11-30");
    expect(moveFocus("2026-10-15", "PageUp", min, max)).toBe("2026-09-23");
    expect(moveFocus("2027-09-20", "ArrowDown", min, max)).toBe("2027-09-23");
    expect(moveFocus("2026-10-15", "Tab", min, max)).toBeNull();
  });

  it("reads dates in Korean and rejects impossible ones", () => {
    expect(fullDateLabel("2026-09-23")).toBe("2026년 9월 23일 수요일");
    expect(isIsoDate("2026-02-30")).toBe(false);
    expect(isIsoDate("20260923")).toBe(false);
  });
});

describe("날짜 선택기", () => {
  afterEach(() => {
    vi.useRealTimers();
    document.body.replaceChildren();
  });

  const mount = (collapsible: boolean) => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 23, 10));
    const root = document.createElement("div");
    document.body.append(root);
    const picker = new DatePicker(root);
    const html = () => picker.render({ id: "d", name: "dep_date", value: "2026-09-25", label: "출발 날짜", collapsible, ...departureRange() });
    root.innerHTML = `<form>${html()}</form>`;
    const changes: string[] = [];
    root.addEventListener("change", (event) => changes.push((event.target as HTMLInputElement).value));
    return { root, picker, html, changes, input: () => root.querySelector<HTMLInputElement>("#d")! };
  };

  it("marks today, the selected day and past days with real buttons", () => {
    const { root } = mount(false);
    expect(root.querySelector("h3")?.textContent).toBe("2026년 9월");
    const today = root.querySelector<HTMLButtonElement>("[aria-current='date']")!;
    expect(today.dataset.dpDay).toBe("2026-09-23");
    expect(today.getAttribute("aria-label")).toBe("2026년 9월 23일 수요일, 오늘");
    const selected = root.querySelector<HTMLElement>("[aria-selected='true'] button")!;
    expect(selected.dataset.dpDay).toBe("2026-09-25");
    expect(selected.tabIndex).toBe(0);
    expect(root.querySelector<HTMLButtonElement>("[data-dp-day='2026-09-22']")!.disabled).toBe(true);
    expect(root.querySelector<HTMLButtonElement>("[data-dp='prev']")!.disabled).toBe(true);
    expect([...root.querySelectorAll("th")].map((cell) => cell.textContent).join("")).toBe("일월화수목금토");
  });

  it("picks a day into the hidden input and announces it with change", () => {
    const { root, changes, input } = mount(false);
    root.querySelector<HTMLButtonElement>("[data-dp-day='2026-09-30']")!.click();
    expect(input().value).toBe("2026-09-30");
    expect(changes).toEqual(["2026-09-30"]);
    expect(document.activeElement?.getAttribute("data-dp-day")).toBe("2026-09-30");
    expect(new FormData(root.querySelector("form")!).get("dep_date")).toBe("2026-09-30");
  });

  it("walks focus across months with the keyboard without picking", () => {
    const { root, input } = mount(false);
    root.querySelector<HTMLButtonElement>("[data-dp-day='2026-09-25']")!.focus();
    for (const key of ["ArrowDown", "ArrowDown"]) {
      document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    }
    expect(root.querySelector("h3")?.textContent).toBe("2026년 10월");
    expect(document.activeElement?.getAttribute("data-dp-day")).toBe("2026-10-09");
    expect(input().value).toBe("2026-09-25");
  });

  it("pages months and keeps its month through a full rerender", () => {
    const { root, picker, html } = mount(false);
    root.querySelector<HTMLButtonElement>("[data-dp='next']")!.click();
    expect(root.querySelector("h3")?.textContent).toBe("2026년 10월");
    root.innerHTML = `<form>${html()}</form>`;
    picker.refresh();
    expect(root.querySelector("h3")?.textContent).toBe("2026년 10월");
  });

  it("follows a value set from outside, such as the 오늘·내일 chips", () => {
    const { root, input } = mount(false);
    input().value = "2026-11-02";
    input().dispatchEvent(new Event("change", { bubbles: true }));
    expect(root.querySelector("h3")?.textContent).toBe("2026년 11월");
    expect(root.querySelector("[aria-selected='true'] button")?.getAttribute("data-dp-day")).toBe("2026-11-02");
  });

  it("opens inline from the form field and folds back once a day is picked", () => {
    const { root, input } = mount(true);
    const toggle = () => root.querySelector<HTMLButtonElement>("[data-dp='toggle']")!;
    expect(root.querySelector(".date-calendar")).toBeNull();
    expect(toggle().textContent).toContain("9월 25일 (금)");
    toggle().click();
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(root.querySelector(".date-calendar.entering")).not.toBeNull();
    root.querySelector<HTMLButtonElement>("[data-dp-day='2026-09-24']")!.click();
    expect(input().value).toBe("2026-09-24");
    expect(root.querySelector(".date-calendar")).toBeNull();
    expect(toggle().textContent).toContain("내일");
    expect(document.activeElement).toBe(toggle());
  });

  it("closes the inline calendar with Escape", () => {
    const { root } = mount(true);
    root.querySelector<HTMLButtonElement>("[data-dp='toggle']")!.click();
    root.querySelector<HTMLButtonElement>("[data-dp-day='2026-09-25']")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(root.querySelector(".date-calendar")).toBeNull();
  });
});
