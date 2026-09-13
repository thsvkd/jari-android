import type {
  BookingPayload,
  Capabilities,
  Conditions,
  RunningSearch,
} from "./types";

export interface BookingDraft {
  depDate: string;
  srcStation: string;
  dstStation: string;
  depTime: string;
  maxDepTime: string;
  unlimitedTime: boolean;
  trainType: "1" | "2";
  seatOption: "1" | "2" | "3" | "4";
  passengerCount: number;
  seatStrategy: "1" | "2";
  seatColumns: string[];
  seatRowMin: string;
  seatRowMax: string;
  waitlist?: boolean;
}

const compact = (value: string): string => value.replaceAll("-", "").replaceAll(":", "");

export function encodeSeatPreference(columns: string[], low: string, high: string): string {
  const normalizedColumns = columns
    .map((column) => column.trim().toUpperCase())
    .filter((column) => ["A", "B", "C", "D"].includes(column));
  const minimum = low.trim();
  const maximum = high.trim();
  if (!normalizedColumns.length && !minimum && !maximum) return "";
  const rows = minimum || maximum ? `${minimum}-${maximum}` : "";
  return `${normalizedColumns.join(",")}:${rows}`;
}

export function buildConditions(draft: BookingDraft): Conditions {
  return {
    v: 1,
    action: "prepare_search",
    dep_date: compact(draft.depDate),
    src_station: draft.srcStation.trim(),
    dst_station: draft.dstStation.trim(),
    dep_time: compact(draft.depTime),
    max_dep_time: draft.unlimitedTime ? "2400" : compact(draft.maxDepTime),
    train_type: draft.trainType,
    seat_option: draft.seatOption,
    passenger_count: Math.min(9, Math.max(1, Math.trunc(draft.passengerCount))),
    seat_strategy: draft.passengerCount === 1 ? "1" : draft.seatStrategy,
    seat_preference: encodeSeatPreference(
      draft.seatColumns,
      draft.seatRowMin,
      draft.seatRowMax,
    ),
    waitlist: draft.waitlist === true,
  };
}

export function buildBookingPayload(conditions: Conditions, trains: string[]): BookingPayload {
  return { conditions, trains: [...trains] };
}

export function normalizeCapabilities(raw: Partial<Capabilities> | null | undefined): Capabilities {
  return {
    korail: raw?.korail === true,
    srt: raw?.srt === true,
    waitlist: raw?.waitlist === true,
    scheduledSearch: raw?.scheduledSearch === true,
    durableNotifications: raw?.durableNotifications === true,
    favourites: raw?.favourites === true,
    notificationSettings: raw?.notificationSettings === true,
    push: raw?.push === true,
    lastChecked: raw?.lastChecked === true,
  };
}

export type ConnectionState = "online" | "offline" | "unknown";
export type RadarKind =
  | "idle"
  | "healthy"
  | "running-unverified"
  | "stale"
  | "error"
  | "offline";

export interface RadarView {
  kind: RadarKind;
  animate: boolean;
  eyebrow: string;
  title: string;
  description: string;
  lastCheckedLabel: string | null;
}

const STALE_AFTER_MS = 120_000;

function formatActualCheck(instant: number): string {
  return new Intl.DateTimeFormat("ko-KR", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(instant));
}

export function deriveRadarView(input: {
  running: RunningSearch | null;
  connection: ConnectionState;
  now?: number;
}): RadarView {
  const { running, connection } = input;
  if (connection !== "online") {
    return {
      kind: "offline",
      animate: false,
      eyebrow: "서버 연결 확인 필요",
      title: "현재 상태를 확인할 수 없어요",
      description: "인터넷 연결이 복구되면 서버 상태를 다시 확인해요.",
      lastCheckedLabel: null,
    };
  }
  if (!running) {
    return {
      kind: "idle",
      animate: false,
      eyebrow: "대기 중",
      title: "진행 중인 검색이 없어요",
      description: "새 여정을 등록하면 서버가 빈자리를 찾아요.",
      lastCheckedLabel: null,
    };
  }

  const checkedAt = running.lastCheckedAt ? Date.parse(running.lastCheckedAt) : Number.NaN;
  const checkedLabel = Number.isFinite(checkedAt) ? formatActualCheck(checkedAt) : null;
  const now = input.now ?? Date.now();

  if (running.health === "error" || running.health === "unavailable") {
    return {
      kind: "error",
      animate: false,
      eyebrow: "조회 문제",
      title: "철도 조회를 완료하지 못했어요",
      description: "검색 기록은 남아 있지만 현재 조회 상태를 확인할 수 없어요.",
      lastCheckedLabel: checkedLabel,
    };
  }
  if (
    running.health === "stale" ||
    (Number.isFinite(checkedAt) && now - checkedAt > STALE_AFTER_MS)
  ) {
    return {
      kind: "stale",
      animate: false,
      eyebrow: "확인 지연",
      title: "한동안 조회 결과가 없어요",
      description: "서버 연결과 검색 상태를 확인해 주세요.",
      lastCheckedLabel: checkedLabel,
    };
  }
  if (
    Number.isFinite(checkedAt) &&
    (running.health === "healthy" || running.health === "running")
  ) {
    return {
      kind: "healthy",
      animate: true,
      eyebrow: "정상 작동",
      title: "빈자리를 찾고 있어요",
      description: "앱을 닫아도 서버에서 계속 검색해요.",
      lastCheckedLabel: checkedLabel,
    };
  }
  return {
    kind: "running-unverified",
    animate: false,
    eyebrow: "검색 실행 기록",
    title: "검색은 서버에 등록돼 있어요",
    description: "최근 조회 상태는 알 수 없어요. 자세한 내용은 검색 상세에서 확인해 주세요.",
    lastCheckedLabel: null,
  };
}

export function conditionsToDraft(conditions?: Conditions | null): BookingDraft {
  const now = new Date();
  now.setDate(now.getDate() + 1);
  const defaultDate = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
  const compactDate = conditions?.dep_date ?? "";
  const [columnPart = "", rowPart = ""] = (conditions?.seat_preference ?? "").split(":");
  const [low = "", high = ""] = rowPart.split("-");
  return {
    depDate:
      compactDate.length === 8
        ? `${compactDate.slice(0, 4)}-${compactDate.slice(4, 6)}-${compactDate.slice(6)}`
        : defaultDate,
    srcStation: conditions?.src_station || "서울",
    dstStation: conditions?.dst_station || "부산",
    depTime: clockFromCompact(conditions?.dep_time || "0700"),
    maxDepTime: clockFromCompact(conditions?.max_dep_time || "1200"),
    unlimitedTime: conditions?.max_dep_time === "2400",
    trainType: conditions?.train_type === "2" ? "2" : "1",
    seatOption: conditions?.seat_option || "1",
    passengerCount: Number(conditions?.passenger_count || 1),
    seatStrategy: conditions?.seat_strategy === "2" ? "2" : "1",
    seatColumns: columnPart.split(",").filter(Boolean),
    seatRowMin: low,
    seatRowMax: high,
    waitlist: conditions?.waitlist === true,
  };
}

export function clockFromCompact(value: string): string {
  return value.length >= 4 ? `${value.slice(0, 2)}:${value.slice(2, 4)}` : value;
}
