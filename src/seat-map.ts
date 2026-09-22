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
  const rows = groupSeatsByLayout(seats);
  const widest = Math.max(0, ...rows.map((row) => row.length));
  for (const row of rows) {
    // A short row by a door or wheelchair space ends mid-carriage, so only full rows say where the windows are.
    if (row.length === widest) {
      window.add(row[0]!.column);
      window.add(row[row.length - 1]!.column);
    }
    row.forEach((seat, index) => {
      const next = row[index + 1];
      if (next && seat.adjacencyGroup && next.adjacencyGroup && seat.adjacencyGroup !== next.adjacencyGroup) {
        aisle.add(seat.column);
        aisle.add(next.column);
      }
    });
  }
  const sorted = (values: Iterable<string>) => [...values].sort((a, b) => a.localeCompare(b));
  // Only seats drawn on the map count; a seat whose label could not be read arrives with no row and no column.
  const columns = new Set(rows.flat().map((seat) => seat.column).filter(Boolean));
  return { columns: sorted(columns), window: sorted(window), aisle: sorted(aisle) };
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

const blocksInGroups = (groups: Map<string, SeatMapSeat[]>, count: number, order: (seat: SeatMapSeat) => number): SeatMapSeat[][] => {
  const results: SeatMapSeat[][] = [];
  for (const group of groups.values()) {
    const ordered = [...group].sort((a, b) => order(a) - order(b));
    for (let index = 0; index <= ordered.length - count; index += 1) {
      const block = ordered.slice(index, index + count);
      if (block.every((seat, offset) => order(seat) === order(block[0]!) + offset)) {
        results.push(block);
      }
    }
  }
  return results;
};

export function consecutiveGroups(seats: SeatMapSeat[], count: number): SeatMapSeat[][] {
  if (count < 1) return [];
  const sideGroups = new Map<string, SeatMapSeat[]>();
  for (const seat of seats) {
    if (!seat.adjacencyGroup) continue;
    const key = `${seat.carNo}:${seat.adjacencyGroup}`;
    const group = sideGroups.get(key) ?? [];
    group.push(seat);
    sideGroups.set(key, group);
  }
  const results = blocksInGroups(sideGroups, count, (seat) => seat.position);
  // 2+2 cars only fit 2 in an adjacencyGroup, so 3+ passengers also get row-wide blocks that may cross the aisle.
  if (count >= 3) {
    const rowGroups = new Map<string, SeatMapSeat[]>();
    for (const seat of seats) {
      if (seat.row === null || seat.rowPosition <= 0) continue;
      const key = `${seat.carNo}:${seat.row}`;
      const group = rowGroups.get(key) ?? [];
      group.push(seat);
      rowGroups.set(key, group);
    }
    results.push(...blocksInGroups(rowGroups, count, (seat) => seat.rowPosition));
  }
  return results;
}
