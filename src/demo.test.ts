import { describe, expect, it, vi } from "vitest";

import { createDemoApi } from "./demo";

describe("isolated demo transport", () => {
  it("supports the journey without making a network request", async () => {
    const network = vi.spyOn(globalThis, "fetch");
    const api = createDemoApi(() => new Date("2026-09-12T00:00:00Z"));

    const bootstrap = await api.bootstrap();
    expect(bootstrap.running).toMatchObject({
      health: "healthy",
      lastCheckedAt: "2026-09-12T00:00:00.000Z",
    });
    const trains = await api.trains({
      conditions: {
        v: 1,
        action: "prepare_search",
        dep_date: "20260919",
        src_station: "서울",
        dst_station: "부산",
        dep_time: "0700",
        max_dep_time: "1200",
        train_type: "1",
        seat_option: "1",
        passenger_count: 1,
        seat_strategy: "1",
        seat_preference: "",
      },
    });
    await api.search({ conditions: bootstrap.draft!, trains: [trains.trains[0]!.no] });
    const status = await api.status();

    expect(bootstrap.demo).toBe(true);
    expect(bootstrap.capabilities.push).toBe(false);
    expect(status.running?.lastCheckedAt).toBe("2026-09-12T00:00:00.000Z");
    expect(network).not.toHaveBeenCalled();
  });
});
