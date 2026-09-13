import { normalizeCapabilities } from "./model";
import type {
  AuthResult,
  BootstrapState,
  Conditions,
  Favourite,
  MobileApi,
  NotificationItem,
  PendingReservation,
  RunningSearch,
  ScheduledSearch,
  SeatInventory,
  SeatMapSeat,
  TrainOption,
} from "./types";

const initialConditions: Conditions = {
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
};

const stations = [
  "서울",
  "용산",
  "광명",
  "천안아산",
  "오송",
  "대전",
  "동대구",
  "부산",
  "울산(통도사)",
  "포항",
  "익산",
  "전주",
  "광주송정",
  "목포",
  "여수EXPO",
  "강릉",
];

const trainFixtures: TrainOption[] = [
  { no: "015", trainKey: "demo-015", label: "07:27→10:12 KTX", dep_time: "072700", arr_time: "101200", name: "KTX", soldout: true, waitlistEligible: true, generalAvailable: false, specialAvailable: false },
  { no: "019", trainKey: "demo-019", label: "08:03→10:48 KTX", dep_time: "080300", arr_time: "104800", name: "KTX", soldout: true, waitlistEligible: false, generalAvailable: false, specialAvailable: false },
  { no: "025", trainKey: "demo-025", label: "09:00→11:42 KTX", dep_time: "090000", arr_time: "114200", name: "KTX", soldout: false, generalAvailable: true, specialAvailable: true },
];

const demoSeats: SeatMapSeat[] = Array.from({ length: 16 }, (_, index) => {
  const row = Math.floor(index / 4) + 1;
  const column = ["A", "B", "C", "D"][index % 4]!;
  return {
    carNo: 3,
    seatNo: `demo-${row}-${column}`,
    label: `${row}${column}`,
    salePossible: row === 1,
    direction: row < 3 ? "1" : "2",
    floor: "",
    row,
    column,
    adjacencyGroup: `${row}:${column < "C" ? "left" : "right"}`,
    position: column === "A" || column === "C" ? 1 : 2,
    familyLabel: row === 2 ? "4인 동반석" : "",
  };
});

const demoInventory = (): SeatInventory => ({
  carNo: 3,
  layoutType: 2,
  arrangementCode: "4",
  remainingCount: demoSeats.filter((seat) => seat.salePossible).length,
  totalCount: demoSeats.length,
  seats: demoSeats.map((seat) => ({ ...seat })),
});

export function createDemoApi(clock: () => Date = () => new Date()): MobileApi {
  let registered = true;
  let running: RunningSearch | null = null;
  let scheduled: ScheduledSearch | null = null;
  let pending: PendingReservation[] = [];
  let favourites: Favourite[] = [
    {
      id: "demo-home",
      name: "주말에 집으로",
      route: "서울 → 부산",
      window: "07:00–12:00",
      conditions: { ...initialConditions, dep_date: "" },
    },
  ];
  let notifyMinutes = 5;
  const notifications: NotificationItem[] = [
    {
      id: "demo-event-1",
      text: "데모 화면이에요. 실제로 열차를 조회하거나 예약하지 않아요.",
      createdAt: clock().toISOString(),
      kind: "demo",
    },
  ];

  const asSearch = (conditions: Conditions, trains: string[]): RunningSearch => ({
    depDate: conditions.dep_date,
    srcLocate: conditions.src_station,
    dstLocate: conditions.dst_station,
    depTime: conditions.dep_time,
    maxDepTime: conditions.max_dep_time,
    trainTypeShow: conditions.train_type === "1" ? "KTX 계열만" : "모든 열차",
    specialInfoShow: ["", "GENERAL_FIRST", "GENERAL_ONLY", "SPECIAL_FIRST", "SPECIAL_ONLY"][Number(conditions.seat_option)] || "GENERAL_FIRST",
    passengerCount: conditions.passenger_count,
    seatStrategy: conditions.seat_strategy === "1" ? "consecutive" : "random",
    seatPreference: conditions.seat_preference,
    selectedTrains: trains,
    startedAt: clock().toISOString(),
    lastCheckedAt: clock().toISOString(),
    health: "healthy",
    attemptCount: 18,
    elapsedSeconds: 1_680,
  });
  running = asSearch(initialConditions, ["015", "019"]);

  const auth = (username: string, role: AuthResult["user"]["role"] = "member"): AuthResult => ({
    token: `demo-${username}`,
    user: { id: "demo-user", username, role },
    expiresAt: new Date(clock().getTime() + 86_400_000).toISOString(),
  });

  const bootstrap = (): BootstrapState => ({
    version: "demo",
    timeZone: "Asia/Seoul",
    user: { id: "demo-user", username: "여행자", role: "member" },
    rail: {
      registered,
      stations,
      majorStations: stations.slice(0, 10),
      displayName: "코레일",
    },
    running,
    scheduled,
    pending,
    favourites,
    notifyMinutes,
    draft: { ...initialConditions },
    paymentUrl: "https://www.letskorail.com/",
    capabilities: normalizeCapabilities({
      korail: true,
      waitlist: true,
      scheduledSearch: true,
      durableNotifications: true,
      favourites: true,
      notificationSettings: true,
      lastChecked: true,
    }),
    pushAvailable: false,
    notifications: { pushAvailable: false },
    demo: true,
  });

  return {
    registerApp: async (input) => auth(input.username, "member"),
    login: async (input) => auth(input.username, input.role ?? "member"),
    createInvite: async () => ({
      invite: "demo-invite-code-not-for-railway",
      ttlHours: 24,
      expiresAt: new Date(clock().getTime() + 86_400_000).toISOString(),
    }),
    logoutApp: async () => ({ ok: true }),
    bootstrap: async () => bootstrap(),
    railwayRegister: async () => {
      registered = true;
      return { registered };
    },
    railwayLogout: async () => {
      registered = false;
      return { registered };
    },
    trains: async () => ({ trains: trainFixtures.map((train) => ({ ...train })), truncated: false, passengerCount: 1 }),
    seatCars: async () => ({
      cars: [{ carNo: 3, roomClassName: "일반실", remainingSeatCount: 4, attributes: [] }],
    }),
    seatInventory: async () => demoInventory(),
    reserveDesignated: async (payload) => {
      pending = [{
        reservationId: "DEMO",
        trainInfo: "체험 데이터 · KTX 025 서울 → 부산",
        expiresAt: new Date(clock().getTime() + 600_000).toISOString(),
        seatNumber: null,
        seatLabels: payload.seats.map((seat) => seat.label),
        seatClass: payload.seatClass,
      }];
      return { reserved: true, pending, paymentUrl: "https://www.letskorail.com/" };
    },
    search: async (payload) => {
      if (payload.conditions.waitlist) {
        return { started: false, waitlisted: true, trainNo: payload.trains[0] };
      }
      running = asSearch(payload.conditions, payload.trains);
      scheduled = null;
      notifications.unshift({
        id: `demo-event-${notifications.length + 1}`,
        text: `${payload.conditions.src_station} → ${payload.conditions.dst_station} 데모 검색을 시작했어요.`,
        createdAt: clock().toISOString(),
        kind: "search",
      });
      return { started: true, running };
    },
    schedule: async (payload) => {
      scheduled = {
        startAt: payload.start_at,
        timeZone: "Asia/Seoul",
        search: asSearch(payload.conditions, payload.trains),
      };
      running = null;
      return { scheduled: true, startAt: payload.start_at };
    },
    cancelSearch: async () => {
      const stopped = running !== null;
      const unscheduled = scheduled !== null;
      running = null;
      scheduled = null;
      return { stopped, unscheduled };
    },
    cancelReservations: async () => {
      pending = [];
      return { cancelled: true, pending };
    },
    requestAccess: async () => ({ requested: true, approved: true }),
    favourites: async () => ({ favourites }),
    saveFavourite: async ({ conditions, name }) => {
      favourites = [
        ...favourites,
        {
          id: `demo-favourite-${favourites.length + 1}`,
          name: name?.trim() || `${conditions.src_station} → ${conditions.dst_station}`,
          route: `${conditions.src_station} → ${conditions.dst_station}`,
          window: `${conditions.dep_time.slice(0, 2)}:${conditions.dep_time.slice(2)}–${conditions.max_dep_time.slice(0, 2)}:${conditions.max_dep_time.slice(2)}`,
          conditions: { ...conditions, dep_date: "" },
        },
      ];
      return { saved: true, favourites };
    },
    deleteFavourite: async (id) => {
      favourites = favourites.filter((item) => item.id !== id);
      return { deleted: true, favourites };
    },
    setNotify: async (minutes) => {
      notifyMinutes = minutes;
      return { notifyMinutes };
    },
    notifications: async () => ({ items: [...notifications], pushAvailable: false }),
    registerDevice: async () => ({ ok: false, pushAvailable: false }),
    deleteDevice: async () => ({ ok: true, pushAvailable: false }),
    status: async () => ({ running, scheduled, pending }),
  };
}
