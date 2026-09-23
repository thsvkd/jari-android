import type {
  BookingPayload,
  Capabilities,
  Conditions,
  RunningSearch,
  SeatClass,
  SeatGradeMode,
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
  seatGradeMode?: SeatGradeMode;
  seatClasses?: SeatClass[];
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
  const specific = draft.seatGradeMode === "specific";
  const classes = [...new Set(draft.seatClasses ?? [])];
  const seatOption = specific
    ? classes.length === 1 && classes[0] === "general"
      ? "2"
      : classes.length === 1 && classes[0] === "special"
        ? "4"
        : "1"
    : draft.seatGradeMode === "any"
      ? "1"
      : draft.seatOption;
  return {
    v: 1,
    action: "prepare_search",
    dep_date: compact(draft.depDate),
    src_station: draft.srcStation.trim(),
    dst_station: draft.dstStation.trim(),
    dep_time: compact(draft.depTime),
    max_dep_time: draft.unlimitedTime ? "2400" : compact(draft.maxDepTime),
    train_type: draft.trainType,
    seat_option: seatOption,
    passenger_count: Math.min(9, Math.max(1, Math.trunc(draft.passengerCount))),
    seat_strategy: draft.passengerCount === 1 ? "1" : draft.seatStrategy,
    seat_preference: encodeSeatPreference(
      draft.seatColumns,
      draft.seatRowMin,
      draft.seatRowMax,
    ),
    waitlist: draft.waitlist === true,
    ...(specific ? { seat_classes: classes } : {}),
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

/** 초 단위 시각은 읽는 사람에게 의미가 없어요. "방금", "3분 전"처럼 지금과의 거리로 말해요. 시각이 없으면 빈 문자열이에요. */
export function relativeTime(iso: string | null | undefined, now: number): string {
  const instant = iso ? Date.parse(iso) : Number.NaN;
  if (!Number.isFinite(instant)) return "";
  const seconds = Math.max(0, Math.round((now - instant) / 1000));
  if (seconds < 60) return "방금";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분 전`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}시간 전`;
  return new Intl.DateTimeFormat("ko-KR", { month: "long", day: "numeric" }).format(new Date(instant));
}

/** 결제 기한까지 남은 시간. 내림으로 세서 실제보다 넉넉해 보이지 않게 하고, 5분 이하면 급하다고 표시해요. */
export function paymentTimeLeft(iso: string, now: number): { text: string; urgent: boolean } {
  const left = Date.parse(iso) - now;
  if (!Number.isFinite(left)) return { text: "", urgent: false };
  if (left <= 0) return { text: "기한 지남", urgent: true };
  const seconds = Math.floor(left / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const text = hours ? `${hours}시간 ${minutes}분 남음` : `${minutes}:${String(seconds % 60).padStart(2, "0")} 남음`;
  return { text, urgent: left <= 5 * 60_000 };
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
      description: connection === "offline" ? "인터넷 연결이 복구되면 서버 상태를 다시 확인해요." : "서버가 응답하면 상태를 다시 확인해요.",
      lastCheckedLabel: null,
    };
  }
  if (!running) {
    return {
      kind: "idle",
      animate: false,
      eyebrow: "대기 중",
      title: "찾고 있는 자리가 없어요",
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
      description: "자리 찾기는 남아 있지만 현재 조회 상태를 확인할 수 없어요.",
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
      description: "서버 연결과 자리 찾기 상태를 확인해 주세요.",
      lastCheckedLabel: checkedLabel,
    };
  }
  // 서버가 상태를 "정상"이라고 말했으면 조회 시각이 아직 없어도 정상으로 봐요. 시각은 그 뒤에 따라와요.
  // "Healthy" with no check time is only believable while the first pass can still be under way. A worker that has
  // been up for minutes and never stamped a check is stuck on a socket, not searching (pit5, 2026-09-22).
  const startedAt = running.startedAt ? Date.parse(running.startedAt) : Number.NaN;
  const silent = !Number.isFinite(checkedAt) && Number.isFinite(startedAt) && now - startedAt > STALE_AFTER_MS;
  if ((running.health === "healthy" || running.health === "running") && silent) {
    return {
      kind: "stale",
      animate: false,
      eyebrow: "확인 지연",
      title: "한동안 확인이 안 됐어요",
      description: "자리 찾기가 멈춘 것 같아요. 그만 찾기 후 다시 시작해 주세요.",
      lastCheckedLabel: null,
    };
  }
  if (running.health === "healthy" || running.health === "running") {
    return {
      kind: "healthy",
      animate: true,
      eyebrow: "정상 작동",
      title: "빈자리를 찾고 있어요",
      description: "앱을 닫아도 서버에서 계속 찾아요.",
      lastCheckedLabel: checkedLabel,
    };
  }
  return {
    kind: "running-unverified",
    animate: false,
    eyebrow: "자리 찾기 기록",
    title: "자리 찾기는 서버에 등록돼 있어요",
    description: "최근 조회 상태는 알 수 없어요. 자세한 내용은 자세히 보기에서 확인해 주세요.",
    lastCheckedLabel: null,
  };
}

export function conditionsToDraft(conditions?: Conditions | null): BookingDraft {
  const now = new Date();
  const defaultDate = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("-");
  const currentMinutes = now.getHours() * 60 + now.getMinutes();
  const roundedStartMinutes = Math.min(23 * 60 + 59, Math.ceil(currentMinutes / 10) * 10);
  const suggestedEndMinutes = roundedStartMinutes + 120;
  const defaultUnlimitedTime = suggestedEndMinutes >= 24 * 60;
  const defaultEndMinutes = Math.min(23 * 60 + 59, suggestedEndMinutes);
  const formatMinutes = (minutes: number) =>
    `${String(Math.floor(minutes / 60)).padStart(2, "0")}:${String(minutes % 60).padStart(2, "0")}`;
  const compactDate = conditions?.dep_date ?? "";
  const [columnPart = "", rowPart = ""] = (conditions?.seat_preference ?? "").split(":");
  const [low = "", high = ""] = rowPart.split("-");
  const explicitClasses = conditions?.seat_classes?.filter(
    (seatClass): seatClass is SeatClass => seatClass === "general" || seatClass === "special",
  );
  const seatClasses: SeatClass[] = explicitClasses?.length
    ? explicitClasses
    : conditions?.seat_option === "2"
      ? ["general"]
      : conditions?.seat_option === "4"
        ? ["special"]
        : [];
  return {
    depDate:
      compactDate.length === 8
        ? `${compactDate.slice(0, 4)}-${compactDate.slice(4, 6)}-${compactDate.slice(6)}`
        : defaultDate,
    srcStation: conditions?.src_station || "서울",
    dstStation: conditions?.dst_station || "부산",
    depTime: conditions ? clockFromCompact(conditions.dep_time || "0700") : formatMinutes(roundedStartMinutes),
    maxDepTime: conditions ? clockFromCompact(conditions.max_dep_time || "1200") : formatMinutes(defaultEndMinutes),
    unlimitedTime: conditions ? conditions.max_dep_time === "2400" : defaultUnlimitedTime,
    trainType: conditions?.train_type === "2" ? "2" : "1",
    seatOption: conditions?.seat_option || "1",
    passengerCount: Number(conditions?.passenger_count || 1),
    seatStrategy: conditions?.seat_strategy === "2" ? "2" : "1",
    seatColumns: columnPart.split(",").filter(Boolean),
    seatRowMin: low,
    seatRowMax: high,
    waitlist: conditions?.waitlist === true,
    seatGradeMode: seatClasses.length ? "specific" : "any",
    seatClasses,
  };
}

export function clockFromCompact(value: string): string {
  return value.length >= 4 ? `${value.slice(0, 2)}:${value.slice(2, 4)}` : value;
}
