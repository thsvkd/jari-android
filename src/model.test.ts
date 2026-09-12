import { describe, expect, it } from "vitest";

import {
  buildBookingPayload,
  buildConditions,
  deriveRadarView,
  normalizeCapabilities,
} from "./model";

describe("booking payload", () => {
  it("serializes every supported gateway condition exactly once", () => {
    const conditions = buildConditions({
      depDate: "2026-09-19",
      srcStation: "서울",
      dstStation: "부산",
      depTime: "07:30",
      maxDepTime: "12:00",
      unlimitedTime: false,
      trainType: "2",
      seatOption: "3",
      passengerCount: 3,
      seatStrategy: "2",
      seatColumns: ["A", "D"],
      seatRowMin: "1",
      seatRowMax: "15",
    });

    expect(conditions).toEqual({
      v: 1,
      action: "prepare_search",
      dep_date: "20260919",
      src_station: "서울",
      dst_station: "부산",
      dep_time: "0730",
      max_dep_time: "1200",
      train_type: "2",
      seat_option: "3",
      passenger_count: 3,
      seat_strategy: "2",
      seat_preference: "A,D:1-15",
    });
    expect(buildBookingPayload(conditions, ["015", "019"])).toEqual({
      conditions,
      trains: ["015", "019"],
    });
  });

  it("uses 2400 for the supported last-train search and normalizes one passenger", () => {
    const conditions = buildConditions({
      depDate: "2026-09-19",
      srcStation: "서울",
      dstStation: "부산",
      depTime: "07:30",
      maxDepTime: "",
      unlimitedTime: true,
      trainType: "1",
      seatOption: "1",
      passengerCount: 1,
      seatStrategy: "2",
      seatColumns: [],
      seatRowMin: "",
      seatRowMax: "",
    });

    expect(conditions.max_dep_time).toBe("2400");
    expect(conditions.seat_strategy).toBe("1");
    expect(conditions.seat_preference).toBe("");
  });
});

describe("capability truth", () => {
  it("defaults unannounced features to unavailable", () => {
    expect(normalizeCapabilities({ scheduledSearch: true })).toEqual({
      korail: false,
      srt: false,
      waitlist: false,
      scheduledSearch: true,
      durableNotifications: false,
      favourites: false,
      notificationSettings: false,
      push: false,
      lastChecked: false,
    });
  });
});

describe("honest radar state", () => {
  const running = {
    depDate: "20260919",
    srcLocate: "서울",
    dstLocate: "부산",
    depTime: "0730",
    maxDepTime: "1200",
    trainTypeShow: "KTX 계열만",
    specialInfoShow: "GENERAL_FIRST",
    passengerCount: 1,
    seatStrategy: "consecutive",
    seatPreference: "",
    selectedTrains: ["015"],
    startedAt: "2026-09-12T00:00:00Z",
  };

  it("does not animate a running record without successful poll telemetry", () => {
    const view = deriveRadarView({ running, connection: "online", now: Date.parse("2026-09-12T00:02:00Z") });

    expect(view.kind).toBe("running-unverified");
    expect(view.animate).toBe(false);
    expect(view.lastCheckedLabel).toBeNull();
  });

  it("animates only a fresh server-reported successful check", () => {
    const view = deriveRadarView({
      running: { ...running, health: "healthy", lastCheckedAt: "2026-09-12T00:01:30Z" },
      connection: "online",
      now: Date.parse("2026-09-12T00:02:00Z"),
    });

    expect(view.kind).toBe("healthy");
    expect(view.animate).toBe(true);
    expect(view.lastCheckedLabel).not.toBeNull();
  });

  it("stops the radar for stale, failed, and offline state", () => {
    expect(
      deriveRadarView({
        running: { ...running, health: "healthy", lastCheckedAt: "2026-09-12T00:00:00Z" },
        connection: "online",
        now: Date.parse("2026-09-12T00:03:01Z"),
      }).kind,
    ).toBe("stale");
    expect(
      deriveRadarView({ running: { ...running, health: "error" }, connection: "online" }).animate,
    ).toBe(false);
    expect(deriveRadarView({ running, connection: "offline" }).kind).toBe("offline");
  });
});
