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

function edgeRow(seats: SeatMapSeat[], edge: "front" | "back"): SeatMapSeat[] {
  const rows = seats.map((seat) => seat.row).filter((row): row is number => row !== null);
  if (!rows.length) return [];
  const target = edge === "front" ? Math.min(...rows) : Math.max(...rows);
  return seats.filter((seat) => seat.row === target);
}

export const frontRowTargets = (seats: SeatMapSeat[]): SeatMapSeat[] => edgeRow(seats, "front");
export const backRowTargets = (seats: SeatMapSeat[]): SeatMapSeat[] => edgeRow(seats, "back");
export const familyTargets = (seats: SeatMapSeat[]): SeatMapSeat[] => seats.filter((seat) => Boolean(seat.familyLabel));

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
