import { afterEach, describe, expect, it, vi } from "vitest";

import { ROW_HEIGHT, TimePicker, clockLabel, hourClock, hourLabel, isClock, timeLabel } from "./time-picker";

describe("시각 계산", () => {
  it("reads clocks the Korean way, with midnight and noon as 12", () => {
    expect(clockLabel("07:00")).toBe("오전 7:00");
    expect(clockLabel("13:05")).toBe("오후 1:05");
    expect(clockLabel("00:30")).toBe("오전 12:30");
    expect(clockLabel("12:00")).toBe("오후 12:00");
    expect(hourLabel(23)).toBe("오후 11시");
    expect(hourLabel(0)).toBe("오전 12시");
    expect(isClock("24:00")).toBe(false);
    expect(isClock("7:00")).toBe(false);
  });

  it("picks whole hours and names them like the wheel, but shows an older off-hour value exactly", () => {
    expect(hourClock(8)).toBe("08:00");
    expect(hourClock(23)).toBe("23:00");
    expect(timeLabel("08:00")).toBe("오전 8시");
    expect(timeLabel("12:00")).toBe("오후 12시");
    expect(timeLabel("05:57")).toBe("오전 5:57");
  });
});

describe("시간 선택기", () => {
  afterEach(() => {
    document.body.replaceChildren();
    vi.useRealTimers();
  });

  // 앱처럼 "이 시간부터"·"이 시간까지" 두 칸을 .time-range 안에 나란히 둬요.
  const mount = ({ start = "07:00", end = "09:00", endDisabled = false } = {}) => {
    const root = document.createElement("div");
    document.body.append(root);
    const picker = new TimePicker(root);
    const html = (next = start) => `<form><div class="time-range">${
      picker.render({ id: "dep", name: "dep_time", value: next, label: "이 시간부터" })
    }${picker.render({ id: "max", name: "max_dep_time", value: end, label: "이 시간까지", disabled: endDisabled })}</div></form>`;
    root.innerHTML = html();
    const changes: string[] = [];
    root.addEventListener("change", (event) => {
      const target = event.target as HTMLInputElement;
      if (target.type === "hidden") changes.push(`${target.name}=${target.value}`);
    });
    const within = (id: string) => root.querySelector<HTMLElement>(`[data-time-picker='${id}']`)!;
    const toggle = (id = "dep") => within(id).querySelector<HTMLButtonElement>("[data-tp='toggle']")!;
    const panel = (id = "dep") => within(id).querySelector(".time-panel");
    const wheel = (id = "dep") => within(id).querySelector<HTMLElement>("[data-tp-wheel]")!;
    const option = (hour: number, id = "dep") => within(id).querySelector<HTMLElement>(`[data-tp-hour='${hour}']`)!;
    const hours = (id = "dep") => [...within(id).querySelectorAll<HTMLElement>("[data-tp-hour]")].map((node) => Number(node.dataset.tpHour));
    const current = (id = "dep") => within(id).querySelector<HTMLElement>(".time-option.current")?.dataset.tpHour;
    const selected = (id = "dep") => [...within(id).querySelectorAll<HTMLElement>("[aria-selected='true']")].map((node) => node.dataset.tpHour);
    // 손가락으로 휠을 넘겨 rows 번째 줄(scrollTop 이 rows 줄)에 둬요. 브라우저처럼 scroll 을 올리고, ended 면 scrollend 도요.
    const flick = (rows: number, { id = "dep", ended = true } = {}) => {
      const element = wheel(id);
      element.dispatchEvent(new Event("pointerdown", { bubbles: true }));
      element.scrollTop = rows * ROW_HEIGHT;
      element.dispatchEvent(new Event("scroll"));
      if (ended) element.dispatchEvent(new Event("scrollend"));
    };
    const key = (key: string) => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    const input = (id = "dep") => root.querySelector<HTMLInputElement>(`#${id}`)!;
    return { root, picker, html, changes, toggle, panel, wheel, option, hours, current, selected, flick, key, input };
  };

  it("shows a whole hour like the wheel and an older off-hour value exactly in a collapsed field", () => {
    const { root, toggle, panel, input } = mount({ start: "05:57" });
    expect(panel()).toBeNull();
    expect(toggle().textContent).toContain("오전 5:57");
    expect(toggle().getAttribute("aria-label")).toBe("이 시간부터 오전 5:57");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(toggle("max").textContent).toContain("오전 9시");
    expect(input().type).toBe("hidden");
    expect(new FormData(root.querySelector("form")!).get("dep_time")).toBe("05:57");
  });

  it("opens a wheel of whole hours in range with the chosen hour in the middle, and no minute control", () => {
    const { root, toggle, panel, wheel, hours, current, selected } = mount({ start: "07:00" });
    toggle().click();
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(root.querySelector(".time-panel.entering")).not.toBeNull();
    expect(hours()).toEqual([5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]);
    expect(wheel().getAttribute("role")).toBe("listbox");
    expect(wheel().textContent).toContain("오전 7시");
    expect(current()).toBe("7");
    expect(selected()).toEqual(["7"]);
    expect(wheel().getAttribute("aria-activedescendant")).toBe("dep-h7");
    // 7시는 5시부터 세 번째 줄이라 두 줄만큼 넘겨 둬야 가운데예요.
    expect(wheel().scrollTop).toBe(2 * ROW_HEIGHT);
    expect(document.activeElement).toBe(wheel());
    expect(panel()!.querySelector("input[type=range], [data-tp-slider]")).toBeNull();
    expect(panel()!.textContent).not.toContain("분");
  });

  it("takes the picker's own hour range", () => {
    const root = document.createElement("div");
    document.body.append(root);
    const picker = new TimePicker(root);
    root.innerHTML = picker.render({ id: "s", value: "", label: "찾기 시작 시각", hours: [0, 23] });
    root.querySelector<HTMLButtonElement>("[data-tp='toggle']")!.click();
    const hours = [...root.querySelectorAll<HTMLElement>("[data-tp-hour]")].map((node) => Number(node.dataset.tpHour));
    expect(hours).toEqual(Array.from({ length: 24 }, (_, hour) => hour));
    expect(root.querySelector("[data-tp-hour='0']")!.textContent).toBe("오전 12시");
  });

  it("takes the hour a flick stops on, on the hour, without closing or redrawing the wheel", () => {
    const { wheel, flick, toggle, input, changes, current, selected, panel } = mount({ start: "07:00" });
    toggle().click();
    const element = wheel();
    flick(8, { ended: false });
    // 넘기는 동안은 가운데 글자만 따라가고 값은 그대로예요.
    expect(current()).toBe("13");
    expect(input().value).toBe("07:00");
    element.dispatchEvent(new Event("scrollend"));
    expect(input().value).toBe("13:00");
    expect(toggle().textContent).toContain("오후 1시");
    expect(toggle().getAttribute("aria-label")).toBe("이 시간부터 오후 1시");
    expect(selected()).toEqual(["13"]);
    expect(wheel()).toBe(element);
    expect(panel()).not.toBeNull();
    expect(changes).toEqual(["dep_time=13:00"]);
  });

  it("settles a flick without scrollend after the wheel has been still for a moment", () => {
    vi.useFakeTimers();
    const { flick, toggle, input } = mount({ start: "07:00" });
    toggle().click();
    flick(1, { ended: false });
    vi.advanceTimersByTime(100);
    expect(input().value).toBe("07:00");
    vi.advanceTimersByTime(100);
    expect(input().value).toBe("06:00");
  });

  it("keeps the hour a flick showed when the field is folded before the wheel settles", () => {
    vi.useFakeTimers();
    const { flick, toggle, input, panel, changes } = mount({ start: "07:00" });
    toggle().click();
    flick(5, { ended: false });
    toggle().click();
    expect(panel()).toBeNull();
    expect(input().value).toBe("10:00");
    vi.advanceTimersByTime(500);
    expect(changes).toEqual(["dep_time=10:00"]);
  });

  it("ignores scrolling it did itself and a flick back to the same hour", () => {
    const { wheel, flick, toggle, input, changes } = mount({ start: "05:57" });
    toggle().click();
    // 펼칠 때 앱이 휠을 제자리로 옮기는 스크롤은 고른 게 아니에요.
    wheel().dispatchEvent(new Event("scroll"));
    wheel().dispatchEvent(new Event("scrollend"));
    // 5시 줄에 도로 멈추면 예전에 분까지 저장한 값을 바꾸지 않아요.
    flick(0);
    expect(input().value).toBe("05:57");
    expect(changes).toEqual([]);
  });

  it("chooses a tapped hour and hands the start over to the end picker", () => {
    const { option, wheel, panel, toggle, input, changes } = mount({ start: "07:00" });
    toggle().click();
    option(8).click();
    expect(input().value).toBe("08:00");
    expect(changes).toEqual(["dep_time=08:00"]);
    expect(panel()).toBeNull();
    expect(panel("max")).not.toBeNull();
    expect(toggle("max").getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(wheel("max"));
  });

  it("closes the end picker when an hour is tapped", () => {
    const { option, panel, toggle, input } = mount();
    toggle("max").click();
    option(11, "max").click();
    expect(input("max").value).toBe("11:00");
    expect(panel("max")).toBeNull();
    expect(document.activeElement).toBe(toggle("max"));
  });

  it("makes a tapped hour whole even when it is the hour already there", () => {
    const { option, toggle, input, changes } = mount({ start: "05:57" });
    toggle().click();
    option(5).click();
    expect(input().value).toBe("05:00");
    expect(changes).toEqual(["dep_time=05:00"]);
  });

  it("steps an hour at a time with the arrow keys and chooses with Enter", () => {
    const { toggle, wheel, key, input, panel, current, flick } = mount({ start: "07:00" });
    toggle().click();
    key("ArrowDown");
    key("ArrowDown");
    expect(input().value).toBe("09:00");
    expect(current()).toBe("9");
    expect(wheel().scrollTop).toBe(4 * ROW_HEIGHT);
    key("ArrowUp");
    expect(input().value).toBe("08:00");
    key("End");
    expect(input().value).toBe("23:00");
    key("ArrowDown");
    expect(input().value).toBe("23:00");
    key("Home");
    expect(input().value).toBe("05:00");
    expect(panel()).not.toBeNull();
    // 손으로 넘겨 둔 뒤 방향키로 고쳐도, 접을 때 넘겨 둔 자리로 되돌아가지 않아요.
    flick(6, { ended: false });
    key("ArrowDown");
    expect(input().value).toBe("06:00");
    key("Enter");
    expect(input().value).toBe("06:00");
    expect(panel()).toBeNull();
    expect(panel("max")).not.toBeNull();
  });

  it("closes after the start when the end picker is off, keeping the end's value out of the form", () => {
    const { root, toggle, option, panel, input } = mount({ start: "07:00", end: "12:40", endDisabled: true });
    expect(toggle("max").disabled).toBe(true);
    expect(input("max").disabled).toBe(true);
    expect(new FormData(root.querySelector("form")!).has("max_dep_time")).toBe(false);
    toggle().click();
    option(8).click();
    expect(panel()).toBeNull();
    expect(panel("max")).toBeNull();
    expect(document.activeElement).toBe(toggle());
  });

  it("starts empty with the current hour in the middle but nothing chosen until a flick or a tap", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 23, 14, 35));
    const { toggle, current, selected, flick, option, input } = mount({ start: "" });
    expect(toggle().textContent).toContain("시각을 골라 주세요");
    toggle().click();
    expect(current()).toBe("14");
    expect(selected()).toEqual([]);
    expect(input().value).toBe("");
    flick(10);
    expect(input().value).toBe("15:00");
    option(16).click();
    expect(input().value).toBe("16:00");
  });

  it("puts the current hour at the nearest end of a range it is outside", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 8, 23, 2, 10));
    const { toggle, current, wheel } = mount({ start: "" });
    toggle().click();
    expect(current()).toBe("5");
    expect(wheel().scrollTop).toBe(0);
  });

  it("stays open through a full rerender with the wheel back on the chosen hour, and takes back a restored value", () => {
    const { root, picker, html, toggle, flick, panel, wheel, current } = mount({ start: "07:00" });
    toggle().click();
    flick(4);
    root.innerHTML = html("09:00");
    expect(wheel().scrollTop).toBe(0);
    picker.refresh();
    expect(panel()).not.toBeNull();
    expect(root.querySelector(".time-panel.entering")).toBeNull();
    expect(wheel().scrollTop).toBe(4 * ROW_HEIGHT);
    expect(current()).toBe("9");
    // 폼 밖 칸은 앱이 숨은 입력 값을 되살린 뒤 refresh() 해요.
    root.innerHTML = html("09:00");
    root.querySelector<HTMLInputElement>("#dep")!.value = "18:00";
    picker.refresh();
    expect(toggle().textContent).toContain("오후 6시");
    expect(wheel().scrollTop).toBe(13 * ROW_HEIGHT);
  });

  it("folds with Escape and keeps only one panel open", () => {
    const { toggle, panel, key } = mount();
    toggle().click();
    toggle("max").click();
    expect(panel()).toBeNull();
    expect(panel("max")).not.toBeNull();
    key("Escape");
    expect(panel("max")).toBeNull();
    expect(document.activeElement).toBe(toggle("max"));
  });
});
