import { afterEach, describe, expect, it } from "vitest";

import { MINUTE_STEP, TimePicker, clockLabel, hourLabel, isClock, withHour, withMinute } from "./time-picker";

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
    expect(withMinute("05:57", 35)).toBe("05:35");
    expect(withMinute("", 10)).toBeNull();
  });
});

describe("시간 선택기", () => {
  afterEach(() => document.body.replaceChildren());

  // 앱처럼 "이 시간부터"·"이 시간까지" 두 칸을 .time-range 안에 나란히 둬요.
  const mount = ({ start = "05:57", end = "09:00", endDisabled = false } = {}) => {
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
    const slider = (part: "hour" | "minute", id = "dep") => within(id).querySelector<HTMLInputElement>(`[data-tp-slider='${part}']`)!;
    const bigButton = (part: "hour" | "minute", id = "dep") => within(id).querySelector<HTMLButtonElement>(`.time-big [data-tp='${part}']`)!;
    const panel = (id = "dep") => within(id).querySelector(".time-panel");
    // 브라우저가 끄는 동안 올리는 input 과 같아요.
    const slide = (part: "hour" | "minute", value: number, id = "dep") => {
      const element = slider(part, id);
      element.value = String(value);
      element.dispatchEvent(new Event("input", { bubbles: true }));
    };
    // 손가락으로 눌러 끌고 떼기.
    const drag = (part: "hour" | "minute", value: number, id = "dep") => {
      slider(part, id).dispatchEvent(new Event("pointerdown", { bubbles: true }));
      slide(part, value, id);
      slider(part, id).dispatchEvent(new Event("pointerup", { bubbles: true }));
    };
    const key = (key: string) => document.activeElement!.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    const input = (id = "dep") => root.querySelector<HTMLInputElement>(`#${id}`)!;
    return { root, picker, html, changes, toggle, slider, bigButton, panel, slide, drag, key, input };
  };

  it("shows an off-step value exactly in a collapsed field", () => {
    const { root, toggle, panel, input } = mount();
    expect(panel()).toBeNull();
    expect(toggle().textContent).toContain("오전 5:57");
    expect(toggle().getAttribute("aria-label")).toBe("이 시간부터 오전 5:57");
    expect(toggle().getAttribute("aria-expanded")).toBe("false");
    expect(input().type).toBe("hidden");
    expect(new FormData(root.querySelector("form")!).get("dep_time")).toBe("05:57");
  });

  it("opens with the time large on top, an hour slider in range and a 5-minute slider, focusing the hour", () => {
    const { root, toggle, slider, bigButton, panel } = mount({ start: "07:35" });
    toggle().click();
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect(root.querySelector(".time-panel.entering")).not.toBeNull();
    expect(panel()!.querySelector(".time-big")!.textContent).toBe("오전 7:35");
    expect(bigButton("hour").textContent).toBe("오전 7");
    expect(bigButton("minute").textContent).toBe("35");
    expect([slider("hour").min, slider("hour").max, slider("hour").step, slider("hour").value]).toEqual(["5", "23", "1", "7"]);
    expect(MINUTE_STEP).toBe(5);
    expect([slider("minute").min, slider("minute").max, slider("minute").step, slider("minute").value]).toEqual(["0", "55", "5", "35"]);
    expect(slider("hour").getAttribute("aria-valuetext")).toBe("오전 7시");
    expect(slider("minute").getAttribute("aria-valuetext")).toBe("35분");
    expect(document.activeElement).toBe(slider("hour"));
  });

  it("takes the picker's own hour range", () => {
    const root = document.createElement("div");
    document.body.append(root);
    const picker = new TimePicker(root);
    root.innerHTML = picker.render({ id: "s", value: "", label: "찾기 시작 시각", hours: [0, 23] });
    root.querySelector<HTMLButtonElement>("[data-tp='toggle']")!.click();
    const hour = root.querySelector<HTMLInputElement>("[data-tp-slider='hour']")!;
    expect([hour.min, hour.max]).toEqual(["0", "23"]);
  });

  it("changes the value while sliding without redrawing the slider, and announces each change", () => {
    const { slider, slide, toggle, input, changes } = mount();
    toggle().click();
    const hour = slider("hour");
    slide("hour", 13);
    expect(slider("hour")).toBe(hour);
    expect(input().value).toBe("13:57");
    expect(toggle().textContent).toContain("오후 1:57");
    expect(hour.getAttribute("aria-valuetext")).toBe("오후 1시");
    slide("minute", 5);
    expect(input().value).toBe("13:05");
    expect(slider("minute").getAttribute("aria-valuetext")).toBe("05분");
    expect(changes).toEqual(["dep_time=13:57", "dep_time=13:05"]);
  });

  it("moves to minutes when the hour is released, then hands the start over to the end picker", () => {
    const { drag, slider, panel, toggle, input } = mount({ start: "07:00" });
    toggle().click();
    drag("hour", 8);
    expect(panel()).not.toBeNull();
    expect(document.activeElement).toBe(slider("minute"));
    drag("minute", 25);
    expect(input().value).toBe("08:25");
    expect(panel()).toBeNull();
    expect(panel("max")).not.toBeNull();
    expect(toggle("max").getAttribute("aria-expanded")).toBe("true");
    expect(document.activeElement).toBe(slider("hour", "max"));
  });

  it("closes the end picker when its minute is released", () => {
    const { drag, panel, toggle, input } = mount();
    toggle("max").click();
    drag("hour", 11, "max");
    drag("minute", 40, "max");
    expect(input("max").value).toBe("11:40");
    expect(panel("max")).toBeNull();
    expect(document.activeElement).toBe(toggle("max"));
  });

  it("advances on a release even when the tap kept the value", () => {
    const { toggle, slider, changes } = mount({ start: "07:00" });
    toggle().click();
    slider("hour").dispatchEvent(new Event("pointerdown", { bubbles: true }));
    document.body.dispatchEvent(new Event("pointerup", { bubbles: true }));
    expect(document.activeElement).toBe(slider("minute"));
    expect(changes).toEqual([]);
  });

  it("does not advance on keyboard changes or a cancelled drag", () => {
    const { toggle, slider, slide, panel, input } = mount({ start: "07:00" });
    toggle().click();
    // 방향키는 브라우저가 값을 바꾸고 input 만 올려요.
    slide("hour", 9);
    slide("minute", 15);
    expect(input().value).toBe("09:15");
    expect(panel()).not.toBeNull();
    expect(document.activeElement).toBe(slider("hour"));
    slider("hour").dispatchEvent(new Event("pointerdown", { bubbles: true }));
    slider("hour").dispatchEvent(new Event("pointercancel", { bubbles: true }));
    slider("hour").dispatchEvent(new Event("pointerup", { bubbles: true }));
    expect(document.activeElement).toBe(slider("hour"));
  });

  it("jumps back to a slider from the big hour or minute", () => {
    const { toggle, drag, slider, bigButton } = mount({ start: "07:00" });
    toggle().click();
    drag("hour", 10);
    expect(bigButton("hour").textContent).toBe("오전 10");
    bigButton("hour").click();
    expect(document.activeElement).toBe(slider("hour"));
    bigButton("minute").click();
    expect(document.activeElement).toBe(slider("minute"));
  });

  it("closes after the start minute when the end picker is off, keeping the end's value out of the form", () => {
    const { root, toggle, drag, panel, input } = mount({ start: "07:00", end: "12:40", endDisabled: true });
    expect(toggle("max").disabled).toBe(true);
    expect(input("max").disabled).toBe(true);
    expect(new FormData(root.querySelector("form")!).has("max_dep_time")).toBe(false);
    toggle().click();
    drag("hour", 8);
    drag("minute", 30);
    expect(panel()).toBeNull();
    expect(panel("max")).toBeNull();
    expect(document.activeElement).toBe(toggle());
  });

  it("starts empty and waits for an hour before minutes", () => {
    const { toggle, slider, bigButton, drag, input } = mount({ start: "" });
    expect(toggle().textContent).toContain("시각을 골라 주세요");
    toggle().click();
    expect(slider("minute").disabled).toBe(true);
    expect(bigButton("minute").disabled).toBe(true);
    drag("hour", 6);
    expect(input().value).toBe("06:00");
    expect(slider("minute").disabled).toBe(false);
    expect(document.activeElement).toBe(slider("minute"));
  });

  it("stays open through a full rerender and takes back a restored value", () => {
    const { root, picker, html, toggle, slide, panel } = mount();
    toggle().click();
    slide("hour", 9);
    root.innerHTML = html("09:57");
    picker.refresh();
    expect(panel()).not.toBeNull();
    expect(root.querySelector(".time-panel.entering")).toBeNull();
    // 폼 밖 칸은 앱이 숨은 입력 값을 되살린 뒤 refresh() 해요.
    root.innerHTML = html("09:57");
    root.querySelector<HTMLInputElement>("#dep")!.value = "18:20";
    picker.refresh();
    expect(toggle().textContent).toContain("오후 6:20");
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
