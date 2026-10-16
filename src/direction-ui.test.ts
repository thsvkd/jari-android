import { afterEach, expect, it, vi } from "vitest";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";
let app: JariApp;
afterEach(() => { app?.dispose(); document.body.replaceChildren(); });
it("sends the chosen direction and removes it when choosing designated seats", async () => {
  const demo = createDemoApi();
  const trains = vi.fn(demo.trains);
  const root = document.createElement("div"); document.body.append(root);
  app = new JariApp(root, { ...demo, trains }, { demoMode: false });
  await app.start(true); app.navigate("journey");
  root.querySelector<HTMLInputElement>("[name='seat_grade_mode'][value='any']")!.click();
  expect(root.querySelector("[name='seat_direction'][value='backward']")).not.toBeNull();
  root.querySelector<HTMLInputElement>("[name='seat_direction'][value='backward']")!.click();
  root.querySelector("#conditions-form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
  await vi.waitFor(() => expect(trains).toHaveBeenCalled());
  expect(trains.mock.calls[0]![0].conditions.seat_direction).toBe("backward");
  await vi.waitFor(() => expect(root.querySelector(".screen-trains")).not.toBeNull());
  expect(root.querySelector("[data-seat-map], [data-official-waitlist]")).toBeNull();
  app.navigate("journey");
  root.querySelector<HTMLInputElement>("[name='seat_grade_mode'][value='specific']")!.click();
  expect(root.querySelector("[name='seat_direction']")).toBeNull();
});

it("hides direction selection when an older server cannot enforce it", async () => {
  const demo = createDemoApi();
  const state = await demo.bootstrap();
  delete (state.capabilities as Partial<typeof state.capabilities>).seatDirection;
  const root = document.createElement("div"); document.body.append(root);
  app = new JariApp(root, { ...demo, bootstrap: async () => state }, { demoMode: false });
  await app.start(true); app.navigate("journey");
  expect(root.querySelector("[name='seat_direction']")).toBeNull();
});
