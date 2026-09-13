export type SearchHealth = "healthy" | "running" | "stale" | "error" | "unavailable" | "unknown";

export interface Capabilities {
  korail: boolean;
  srt: boolean;
  waitlist: boolean;
  scheduledSearch: boolean;
  durableNotifications: boolean;
  favourites: boolean;
  notificationSettings: boolean;
  push: boolean;
  lastChecked: boolean;
}

export type AppRole = "admin" | "member";

export interface AppUser {
  id: string;
  username: string;
  role?: AppRole;
}

export interface AuthResult {
  token: string;
  user: AppUser;
  expiresAt: string;
}

export interface RailState {
  registered: boolean;
  stations: string[];
  majorStations: string[];
  displayName: string;
}

export interface Conditions {
  v: 1;
  action: "prepare_search";
  dep_date: string;
  src_station: string;
  dst_station: string;
  dep_time: string;
  max_dep_time: string;
  train_type: "1" | "2";
  seat_option: "1" | "2" | "3" | "4";
  passenger_count: number;
  seat_strategy: "1" | "2";
  seat_preference: string;
  waitlist?: boolean;
  trains?: string[];
  seat_classes?: SeatClass[];
  seat_plan?: CancellationWaitPlan;
}

export interface SearchDescription {
  depDate: string;
  srcLocate: string;
  dstLocate: string;
  depTime: string;
  maxDepTime: string;
  trainTypeShow: string;
  specialInfoShow: string;
  passengerCount: number;
  seatStrategy: string;
  seatPreference: string;
  selectedTrains: string[];
}

export interface RunningSearch extends SearchDescription {
  startedAt: string | null;
  health?: SearchHealth;
  lastCheckedAt?: string | null;
  attemptCount?: number | null;
  elapsedSeconds?: number | null;
}

export interface ScheduledSearch {
  startAt: string | null;
  timeZone: string;
  search: SearchDescription;
}

export interface PendingReservation {
  reservationId: string | null;
  trainInfo: string;
  expiresAt: string | null;
  seatNumber: string | number | null;
  seatLabels?: string[];
  seatClass?: SeatClass | "";
}

export interface Favourite {
  id: string;
  name: string;
  route: string;
  window: string;
  conditions: Conditions;
}

export interface NotificationItem {
  id: string;
  text: string;
  createdAt: string;
  kind: string;
}

export interface BootstrapState {
  version: string;
  timeZone: string;
  user?: AppUser;
  rail: RailState;
  running: RunningSearch | null;
  scheduled: ScheduledSearch | null;
  pending: PendingReservation[];
  favourites: Favourite[];
  notifyMinutes: number;
  draft: Conditions | null;
  paymentUrl: string;
  capabilities: Capabilities;
  pushAvailable?: boolean;
  notifications?: { pushAvailable: boolean };
  demo?: boolean;
}

export interface TrainOption {
  no: string;
  label: string;
  dep_time?: string;
  arr_time?: string;
  name?: string;
  soldout: boolean;
  waitlistEligible?: boolean;
  trainKey?: string;
  generalAvailable?: boolean;
  specialAvailable?: boolean;
}

export type SeatClass = "general" | "special";
export type SeatGradeMode = "any" | "specific";
export type SeatSelectionMode = "immediate" | "wait";

export interface SeatCarOption {
  carNo: number;
  roomClassName: string;
  remainingSeatCount: number;
  attributes: Array<{ name: string; code: string | null }>;
}

export interface SeatMapSeat {
  carNo: number;
  seatNo: string;
  label: string;
  salePossible: boolean;
  direction: string;
  floor: string;
  row: number | null;
  column: string;
  adjacencyGroup: string;
  position: number;
  familyLabel: string;
}

export interface SeatInventory {
  carNo: number;
  layoutType: number;
  arrangementCode: string;
  remainingCount: number;
  totalCount: number;
  seats: SeatMapSeat[];
  windows?: Array<{ startLocationRatio: number; closeLocationRatio: number }>;
}

export interface TrainSeatTargets {
  trainNo: string;
  trainKey?: string;
  seatClass: SeatClass;
  targets: SeatMapSeat[];
}

export interface CancellationWaitPlan {
  strategy: "independent" | "consecutive";
  passengerCount: number;
  trains: TrainSeatTargets[];
}

export interface DesignatedReservationResult {
  reserved: boolean;
  pending: PendingReservation[];
  paymentUrl: string;
}

export interface BookingPayload {
  conditions: Conditions;
  trains: string[];
}

export interface TrainsResult {
  trains: TrainOption[];
  truncated: boolean;
  passengerCount: number;
}

export interface SearchResult {
  started: boolean;
  waitlisted?: boolean;
  trainNo?: string;
  needsAccessRequest?: boolean;
  accessRequestPending?: boolean;
  trialUsed?: number | null;
  trialLimit?: number | null;
  running?: RunningSearch | null;
}

export interface StatusResult {
  running: RunningSearch | null;
  scheduled: ScheduledSearch | null;
  pending: PendingReservation[];
}

export interface InviteResult {
  invite: string;
  ttlHours: number;
  expiresAt: string;
}

export interface MobileApi {
  registerApp(input: { username: string; password: string; invite: string }): Promise<AuthResult>;
  login(input: { username: string; password: string; role?: AppRole }): Promise<AuthResult>;
  createInvite(input?: { ttlHours?: number }): Promise<InviteResult>;
  logoutApp(): Promise<{ ok: boolean }>;
  bootstrap(): Promise<BootstrapState>;
  railwayRegister(input: { username: string; password: string }): Promise<{ registered: boolean }>;
  railwayLogout(): Promise<{ registered: boolean }>;
  trains(payload: { conditions: Conditions }): Promise<TrainsResult>;
  seatCars(trainKey: string, seatClass: SeatClass, passengerCount: number): Promise<{ cars: SeatCarOption[] }>;
  seatInventory(trainKey: string, carNo: number, seatClass: SeatClass, passengerCount: number): Promise<SeatInventory>;
  reserveDesignated(payload: {
    trainKey: string;
    seatClass: SeatClass;
    passengerCount: number;
    carNo: number;
    seats: SeatMapSeat[];
  }): Promise<DesignatedReservationResult>;
  search(payload: BookingPayload): Promise<SearchResult>;
  schedule(payload: BookingPayload & { start_at: string }): Promise<{ scheduled: boolean; startAt: string }>;
  cancelSearch(): Promise<{ stopped: boolean; unscheduled: boolean }>;
  cancelReservations(): Promise<{ cancelled: boolean; pending: PendingReservation[] }>;
  requestAccess(): Promise<{ requested: boolean; approved?: boolean }>;
  favourites(): Promise<{ favourites: Favourite[] } | Favourite[]>;
  saveFavourite(payload: { conditions: Conditions; name?: string }): Promise<{ saved: boolean; favourites: Favourite[] }>;
  deleteFavourite(id: string): Promise<{ deleted: boolean; favourites: Favourite[] }>;
  setNotify(minutes: number): Promise<{ notifyMinutes: number }>;
  notifications(): Promise<{ items: NotificationItem[]; pushAvailable: boolean }>;
  registerDevice(token: string): Promise<{ ok?: boolean; pushAvailable?: boolean }>;
  deleteDevice(token: string): Promise<{ ok?: boolean; pushAvailable?: boolean }>;
  status(): Promise<StatusResult>;
}
