import { describe, expect, it } from "vitest";

import {
  consecutiveGroups,
  filterSeats,
  groupSeatsByLayout,
  maxTrimRows,
  seatColumnSets,
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

  it("filters by column, trimmed end rows, and companion seats", () => {
    const labels = (items: SeatMapSeat[]) => items.map((item) => item.label);
    const threeRows = [
      ...seats,
      seat(3, "A", "left", 1), seat(3, "B", "left", 2),
      seat(3, "C", "right", 1), seat(3, "D", "right", 2),
    ];
    expect(labels(filterSeats(seats, { columns: ["A", "D"], trimRows: 0, excludeFamily: false }))).toEqual(["1A", "1D", "2A", "2D"]);
    expect(labels(filterSeats(seats, { columns: [], trimRows: 0, excludeFamily: true }))).toEqual(["1A", "1B", "1C", "1D", "2C", "2D"]);
    expect(maxTrimRows(seats)).toBe(0);
    expect(maxTrimRows(threeRows)).toBe(1);
    expect(labels(filterSeats(threeRows, { columns: ["A"], trimRows: 1, excludeFamily: false }))).toEqual(["2A"]);
  });

  it("reads window and aisle columns from the layout, including a 1+2 car", () => {
    expect(seatColumnSets(seats)).toEqual({ columns: ["A", "B", "C", "D"], window: ["A", "D"], aisle: ["B", "C"] });
    const special = [seat(1, "A", "solo", 1), seat(1, "B", "pair", 1), seat(1, "C", "pair", 2)];
    expect(seatColumnSets(special)).toEqual({ columns: ["A", "B", "C"], window: ["A", "C"], aisle: ["A", "B"] });
  });

  it("never joins seats across an aisle, row, or car", () => {
    expect(consecutiveGroups(seats, 2).map((group) => group.map((item) => item.label))).toEqual([
      ["1A", "1B"], ["1C", "1D"], ["2A", "2B"], ["2C", "2D"],
    ]);
    expect(consecutiveGroups(seats, 3)).toEqual([]);
  });
});
