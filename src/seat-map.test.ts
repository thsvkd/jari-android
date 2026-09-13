import { describe, expect, it } from "vitest";

import {
  backRowTargets,
  consecutiveGroups,
  familyTargets,
  frontRowTargets,
  groupSeatsByLayout,
} from "./seat-map";
import type { SeatMapSeat } from "./types";

const seat = (row: number, column: string, group: string, position: number, familyLabel = ""): SeatMapSeat => ({
  carNo: 3,
  seatNo: `${row}-${column}`,
  label: `${row}${column}`,
  salePossible: row === 2,
  direction: "1",
  floor: "",
  row,
  column,
  adjacencyGroup: `${row}:${group}`,
  position,
  familyLabel,
});

describe("dynamic Korail seat layouts", () => {
  const seats = [
    seat(1, "A", "left", 1), seat(1, "B", "left", 2),
    seat(1, "C", "right", 1), seat(1, "D", "right", 2),
    seat(2, "A", "left", 1, "4인 동반석"), seat(2, "B", "left", 2, "4인 동반석"),
    seat(2, "C", "right", 1), seat(2, "D", "right", 2),
  ];

  it("groups four-column and three-column cars from returned row and column values", () => {
    expect(groupSeatsByLayout(seats).map((row) => row.map((item) => item.column))).toEqual([
      ["A", "B", "C", "D"],
      ["A", "B", "C", "D"],
    ]);
    expect(groupSeatsByLayout(seats.filter((item) => item.column !== "D"))[0]).toHaveLength(3);
  });

  it("finds the actual first, last, and explicitly marked companion rows", () => {
    expect(frontRowTargets(seats).map((item) => item.label)).toEqual(["1A", "1B", "1C", "1D"]);
    expect(backRowTargets(seats).map((item) => item.label)).toEqual(["2A", "2B", "2C", "2D"]);
    expect(familyTargets(seats).map((item) => item.label)).toEqual(["2A", "2B"]);
  });

  it("never joins seats across an aisle, row, or car", () => {
    expect(consecutiveGroups(seats, 2).map((group) => group.map((item) => item.label))).toEqual([
      ["1A", "1B"], ["1C", "1D"], ["2A", "2B"], ["2C", "2D"],
    ]);
    expect(consecutiveGroups(seats, 3)).toEqual([]);
  });
});
