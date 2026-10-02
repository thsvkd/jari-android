import { describe, expect, it } from "vitest";

import {
  consecutiveGroups,
  familyQuartets,
  filterSeats,
  groupSeatsByLayout,
  limitedFamilySeats,
  maxTrimRows,
  seatColumnSets,
} from "./seat-map";
import type { SeatMapSeat } from "./types";

const seat = (row: number, column: string, group: string, position: number, familyLabel = "", rowPosition = 0): SeatMapSeat => ({
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
  rowPosition,
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
    expect(labels(filterSeats(seats, { columns: ["A", "D"], trimRows: 0, onlyFamily: false, excludeFamily: false }))).toEqual(["1A", "1D", "2A", "2D"]);
    expect(labels(filterSeats(seats, { columns: [], trimRows: 0, onlyFamily: false, excludeFamily: true }))).toEqual(["1A", "1B", "1C", "1D", "2C", "2D"]);
    expect(labels(filterSeats(seats, { columns: [], trimRows: 0, onlyFamily: true, excludeFamily: false }))).toEqual(["2A", "2B"]);
    expect(maxTrimRows(seats)).toBe(0);
    expect(maxTrimRows(threeRows)).toBe(1);
    expect(labels(filterSeats(threeRows, { columns: ["A"], trimRows: 1, onlyFamily: false, excludeFamily: false }))).toEqual(["2A"]);
  });

  it("reads window and aisle columns from the layout, including a 1+2 car", () => {
    expect(seatColumnSets(seats)).toEqual({ columns: ["A", "B", "C", "D"], window: ["A", "D"], aisle: ["B", "C"] });
    const shortRow = [...seats, seat(3, "A", "left", 1), seat(3, "B", "left", 2)];
    expect(seatColumnSets(shortRow).window).toEqual(["A", "D"]);
    const unreadable = { ...seat(1, "", "", 0), row: null, label: "?" };
    expect(seatColumnSets([...seats, unreadable]).columns).toEqual(["A", "B", "C", "D"]);
    const special = [seat(1, "A", "solo", 1), seat(1, "B", "pair", 1), seat(1, "C", "pair", 2)];
    expect(seatColumnSets(special)).toEqual({ columns: ["A", "B", "C"], window: ["A", "C"], aisle: ["A", "B"] });
  });

  it("never joins seats across an aisle, row, or car", () => {
    expect(consecutiveGroups(seats, 2).map((group) => group.map((item) => item.label))).toEqual([
      ["1A", "1B"], ["1C", "1D"], ["2A", "2B"], ["2C", "2D"],
    ]);
    // No rowPosition on this fixture, so 3+ finds no row-wide blocks either (only 2 seats per adjacencyGroup side).
    expect(consecutiveGroups(seats, 3)).toEqual([]);
  });

  it("also matches row-wide blocks across the aisle for 3+ passengers", () => {
    const labels = (groups: SeatMapSeat[][]) => groups.map((group) => group.map((item) => item.label));
    const threeAcross = [
      seat(1, "A", "left", 1, "", 1), seat(1, "B", "left", 2, "", 2), seat(1, "C", "right", 1, "", 3),
    ];
    expect(labels(consecutiveGroups(threeAcross, 3))).toEqual([["1A", "1B", "1C"]]);

    const spansRows = [
      seat(1, "A", "left", 1, "", 1), seat(1, "B", "left", 2, "", 2),
      seat(2, "A", "left", 1, "", 1),
    ];
    expect(consecutiveGroups(spansRows, 3)).toEqual([]);

    const twoAcrossAisle = [seat(1, "B", "left", 2, "", 2), seat(1, "C", "right", 1, "", 3)];
    expect(consecutiveGroups(twoAcrossAisle, 2)).toEqual([]);
  });

  it("treats a facing family set as one block and leaves a single family row as a row", () => {
    const labels = (groups: SeatMapSeat[][]) => groups.map((group) => group.map((item) => item.label));
    const family = (row: number, column: string, side: string, position: number, rowPosition: number) =>
      seat(row, column, side, position, "4인 동반석", rowPosition);
    const facing = [7, 8].flatMap((row) => [
      family(row, "A", "left", 1, 1), family(row, "B", "left", 2, 2),
      family(row, "C", "right", 1, 3), family(row, "D", "right", 2, 4),
    ]);
    expect(labels(familyQuartets(facing))).toEqual([
      ["7A", "7B", "8A", "8B"],
      ["7C", "7D", "8C", "8D"],
    ]);
    // The same-row blocks would book half of each set, so they are dropped.
    expect(labels(consecutiveGroups(facing, 4))).toEqual([
      ["7A", "7B", "8A", "8B"],
      ["7C", "7D", "8C", "8D"],
    ]);
    const oneRow = [
      family(2, "A", "left", 1, 1), family(2, "B", "left", 2, 2),
      family(2, "C", "right", 1, 3), family(2, "D", "right", 2, 4),
    ];
    expect(familyQuartets(oneRow)).toEqual([]);
    expect(labels(consecutiveGroups(oneRow, 4))).toEqual([["2A", "2B", "2C", "2D"]]);
    // A hyphen group is not a side, so it cannot become a quartet.
    const hyphen = oneRow.map((item) => ({ ...item, adjacencyGroup: `${item.row}-left` }));
    expect(familyQuartets(hyphen)).toEqual([]);
    const saleable = facing.map((item) => ({ ...item, salePossible: item.label !== "8D" }));
    const open = saleable.filter((item) => item.salePossible);
    expect(labels([limitedFamilySeats(open, { columns: [], trimRows: 0, onlyFamily: true, excludeFamily: false }, 4)])).toEqual([
      ["7A", "7B", "8A", "8B"],
    ]);
    expect(limitedFamilySeats(open, { columns: [], trimRows: 0, onlyFamily: false, excludeFamily: false }, 4).map((item) => item.label)).toEqual([
      "7A", "7B", "7C", "7D",
    ]);
  });
});
