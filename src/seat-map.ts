import type { SeatMapSeat } from "./types";

export function groupSeatsByLayout(seats: SeatMapSeat[]): SeatMapSeat[][] {
  const rows = new Map<number, SeatMapSeat[]>();
  for (const seat of seats) {
    if (seat.row === null) continue;
    const row = rows.get(seat.row) ?? [];
    row.push(seat);
    rows.set(seat.row, row);
  }
  return [...rows.entries()]
    .sort(([a], [b]) => a - b)
    .map(([, row]) => row.sort((a, b) => a.column.localeCompare(b.column)));
}

export interface SeatFilter {
  columns: string[];
  trimRows: number;
  excludeFamily: boolean;
}

export const emptySeatFilter = (): SeatFilter => ({ columns: [], trimRows: 0, excludeFamily: false });

const distinctRows = (seats: SeatMapSeat[]): number[] =>
  [...new Set(seats.map((seat) => seat.row).filter((row): row is number => row !== null))].sort((a, b) => a - b);

/** Columns in the car, and which of them sit at a window or beside the aisle, read from the layout itself
 *  so a 1+2 special car gets its own answer instead of assuming A·D are windows. */
export function seatColumnSets(seats: SeatMapSeat[]): { columns: string[]; window: string[]; aisle: string[] } {
  const window = new Set<string>();
  const aisle = new Set<string>();
  for (const row of groupSeatsByLayout(seats)) {
    window.add(row[0]!.column);
    window.add(row[row.length - 1]!.column);
    row.forEach((seat, index) => {
      const next = row[index + 1];
      if (next && seat.adjacencyGroup && next.adjacencyGroup && seat.adjacencyGroup !== next.adjacencyGroup) {
        aisle.add(seat.column);
        aisle.add(next.column);
      }
    });
  }
  const sorted = (values: Iterable<string>) => [...values].sort((a, b) => a.localeCompare(b));
  return { columns: sorted(new Set(seats.map((seat) => seat.column))), window: sorted(window), aisle: sorted(aisle) };
}

/** Most rows that can be trimmed from each end while leaving at least one row. */
export const maxTrimRows = (seats: SeatMapSeat[]): number => Math.max(0, Math.floor((distinctRows(seats).length - 1) / 2));

export function filterSeats(seats: SeatMapSeat[], filter: SeatFilter): SeatMapSeat[] {
  const rows = distinctRows(seats);
  const kept = new Set(rows.slice(filter.trimRows, rows.length - filter.trimRows));
  return seats.filter((seat) =>
    seat.row !== null
    && kept.has(seat.row)
    && (!filter.columns.length || filter.columns.includes(seat.column))
    && !(filter.excludeFamily && seat.familyLabel));
}

export function consecutiveGroups(seats: SeatMapSeat[], count: number): SeatMapSeat[][] {
  if (count < 1) return [];
  const groups = new Map<string, SeatMapSeat[]>();
  for (const seat of seats) {
    if (!seat.adjacencyGroup) continue;
    const key = `${seat.carNo}:${seat.adjacencyGroup}`;
    const group = groups.get(key) ?? [];
    group.push(seat);
    groups.set(key, group);
  }
  const results: SeatMapSeat[][] = [];
  for (const group of groups.values()) {
    const ordered = [...group].sort((a, b) => a.position - b.position);
    for (let index = 0; index <= ordered.length - count; index += 1) {
      const block = ordered.slice(index, index + count);
      if (block.every((seat, offset) => seat.position === block[0]!.position + offset)) {
        results.push(block);
      }
    }
  }
  return results;
}
