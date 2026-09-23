import { afterEach, describe, expect, it } from "vitest";

import { TimePicker, clockLabel, hourGroups, hourLabel, isClock, moveInRows, withHour, withMinute } from "./time-picker";

describe("시각 계산", () => {
  it("reads clocks the Korean way, with midnight and noon as 12", () => {
    expect(clockLabel("07:00")).toBe("오전 7:00");
    expect(clockLabel("13:05")).toBe("오후 1:05");
    expect(clockLabel("00:30")).toBe("오전 12:30");
    expect(clockLabel("12:00")).toBe("오후 12:00");
    expect(hourLabel(23)).toBe("오후 11시");
    expect(isClock("24:00")).toBe(false);
    expect(isClock("7:00")).toBe(false);
  });

  it("changes the hour but keeps an off-step minute, and needs an hour before a minute", () => {
    expect(withHour("05:57", 8)).toBe("08:57");
    expect(withHour("", 8)).toBe("08:00");
    expect(withMinute("05:57", 10)).toBe("05:10");
    expect(withMinute("", 10)).toBeNull();
  });

  it("groups the train hours 05–23 into 오전·오후 rows of four", () => {
    expect(hourGroups(5, 23)).toEqual([
      { label: "오전", rows: [[5, 6, 7, 8], [9, 10, 11, null]] },
      { label: "오후", rows: [[12, 13, 14, 15], [16, 17, 18, 19], [20, 21, 22, 23]] },
    ]);
    expect(hourGroups(0, 23).map((group) => group.rows.length)).toEqual([3, 3]);
  });

  it("moves along rows and down columns across 오전·오후, staying put at the edges", () => {
    const rows = hourGroups(5, 23).flatMap((group) => group.rows);
    expect(moveInRows(rows, 7, "ArrowRight")).toBe(8);
    expect(moveInRows(rows, 11, "ArrowRight")).toBe(12);
    expect(moveInRows(rows, 5, "ArrowLeft")).toBe(5);
    expect(moveInRows(rows, 10, "ArrowDown")).toBe(13);
    expect(moveInRows(rows, 15, "ArrowUp")).toBe(8);
    expect(moveInRows(rows, 21, "ArrowDown")).toBe(21);
    expect(moveInRows(rows, 14, "End")).toBe(23);
    expect(moveInRows(rows, 14, "Home")).toBe(5);
    expect(moveInRows(rows, 14, "Tab")).toBeNull();
  });
});

describe("시간 선택기", () => {
  afterEach(() => document.body.replaceChildren());

  const mount = (value = "05:57", disabled = false) => {
    const root = document.createElement("div");
    document.body.append(root);
    const picker = new TimePicker(root);
    const html = (next = value) => picker.render({ id: "t", name: "dep_time", value: next, label: "이 시간부터", disabled });
    root.innerHTML = `<form>${html()}</form>`;
    const changes: string[] = [];
    root.addEventListener("change", (event) => changes.push((event.target as HTMLInputElement).value));
    const toggle = () => root.querySelector<HTMLButtonElement>("[data-tp='toggle']")!;
    const hour = (h: number) => root.querySelector<HTMLButtonElement>(`[data-tp-hour='${h}']`)!;
    const minute = (m: number) => root.querySelector<HTMLButtonElement>(`[data-tp-minute='${m}']`)!;
    const key = (key: string) => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    return { root, picker, html, changes, toggle, hour, minute, key, input: () => root.querySelector<HTMLInputElement>("#t")! };
  };

  it("shows an off-step value exactly in a collapsed field", () => {
    const { root, toggle, input } = mount();
    expect(root.querySelector(".time-panel")).toBeNull();
    expect(toggle().textContent).toContain("오전 5:57");
    expect(toggle().getAttribute("aria-label")).toBe("이 시간부터 오전 5:57");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(input().type).toBe("hidden");
    expect(new FormData(root.querySelector("form")!).get("dep_time")).toBe("05:57");
  });

  it("opens with 오전·오후 hour chips and 10-minute chips, pressing the current hour only", () => {
    const { root, toggle, hour, minute } = mount();
    toggle().click();
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(root.querySelector(".time-panel.entering")).not.toBeNull();
    expect([...root.querySelectorAll(".time-panel > p")].map((label) => label.textContent)).toEqual(["오전", "오후", "분"]);
    expect(root.querySelectorAll("[data-tp-hour]")).toHaveLength(19);
    expect(hour(13).textContent).toBe("1시");
    expect(hour(13).getAttribute("aria-label")).toBe("오후 1시");
    expect(hour(5).getAttribute("aria-pressed")).toBe("true");
    expect(hour(5).tabIndex).toBe(0);
    expect(root.querySelectorAll("[data-tp-minute][aria-pressed='true']")).toHaveLength(0);
    expect(minute(0).tabIndex).toBe(0);
  });

  it("keeps the panel open after an hour, folds after a minute, and announces each with change", () => {
    const { root, toggle, hour, minute, input, changes } = mount();
    toggle().click();
    hour(7).click();
    expect(input().value).toBe("07:57");
    expect(root.querySelector(".time-panel")).not.toBeNull();
    expect(document.activeElement).toBe(hour(7));
    expect(hour(7).getAttribute("aria-pressed")).toBe("true");
    minute(30).click();
    expect(input().value).toBe("07:30");
    expect(root.querySelector(".time-panel")).toBeNull();
    expect(document.activeElement).toBe(toggle());
    expect(toggle().textContent).toContain("오전 7:30");
    expect(changes).toEqual(["07:57", "07:30"]);
  });

  it("stays open through a full rerender and takes back a restored value", () => {
    const { root, picker, html, toggle, hour } = mount();
    toggle().click();
    hour(9).click();
    root.innerHTML = `<form>${html("09:57")}</form>`;
    picker.refresh();
    expect(root.querySelector(".time-panel")).not.toBeNull();
    expect(root.querySelector(".time-panel.entering")).toBeNull();
    expect(hour(9).getAttribute("aria-pressed")).toBe("true");
    // 폼 밖 칸은 앱이 숨은 입력 값을 되살린 뒤 refresh() 해요.
    root.innerHTML = `<form>${html("09:57")}</form>`;
    root.querySelector<HTMLInputElement>("#t")!.value = "18:20";
    picker.refresh();
    expect(toggle().textContent).toContain("오후 6:20");
  });

  it("starts empty without a value and waits for an hour before minutes", () => {
    const { toggle, hour, minute, input } = mount("");
    expect(toggle().textContent).toContain("시각을 골라 주세요");
    toggle().click();
    expect(minute(10).disabled).toBe(true);
    hour(6).click();
    expect(input().value).toBe("06:00");
    expect(minute(0).getAttribute("aria-pressed")).toBe("true");
  });

  it("is disabled with its hidden input, so the form leaves it out", () => {
    const { root, toggle, input } = mount("12:40", true);
    expect(toggle().disabled).toBe(true);
    expect(input().disabled).toBe(true);
    expect(new FormData(root.querySelector("form")!).has("dep_time")).toBe(false);
    expect(input().value).toBe("12:40");
  });

  it("walks chips with arrow keys without picking and folds with Escape", () => {
    const { root, toggle, hour, minute, key, input } = mount("07:00");
    toggle().click();
    hour(7).focus();
    key("ArrowDown");
    key("ArrowRight");
    expect(document.activeElement).toBe(hour(12));
    expect(hour(12).tabIndex).toBe(0);
    expect(hour(7).tabIndex).toBe(-1);
    minute(0).focus();
    key("End");
    expect(document.activeElement).toBe(minute(50));
    expect(input().value).toBe("07:00");
    key("Escape");
    expect(root.querySelector(".time-panel")).toBeNull();
    expect(document.activeElement).toBe(toggle());
  });

  it("keeps only one panel open when two pickers sit side by side", () => {
    const root = document.createElement("div");
    document.body.append(root);
    const picker = new TimePicker(root);
    root.innerHTML = picker.render({ id: "a", value: "07:00", label: "이 시간부터" }) + picker.render({ id: "b", value: "09:00", label: "이 시간까지" });
    root.querySelector<HTMLButtonElement>("[data-time-picker='a'] [data-tp='toggle']")!.click();
    root.querySelector<HTMLButtonElement>("[data-time-picker='b'] [data-tp='toggle']")!.click();
    expect([...root.querySelectorAll(".time-picker.open")].map((element) => element.getAttribute("data-time-picker"))).toEqual(["b"]);
  });
});
