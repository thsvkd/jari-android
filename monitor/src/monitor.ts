/**
 * 자리났다 API 외부 감시. Cloudflare Worker 의 cron 이 1분마다 운영 /health 를 앱과 같은 길(Cloudflare → 터널 → Caddy → API)로
 * 불러 보고, 장애가 되고 풀릴 때만 텔레그램으로 알려요. pit5 밖에서 돌아서 호스트가 통째로 죽어도 알림이 와요.
 */

export interface KV {
  get(key: string): Promise<string | null>;
  put(key: string, value: string): Promise<void>;
}

export interface Env {
  STATE: KV;
  HEALTH_URL: string;
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID: string;
}

export interface Probe {
  ok: boolean;
  /** 사람이 읽을 원인. 정상이면 "ok". */
  detail: string;
}

export interface State {
  status: "up" | "down";
  /** 연속 실패 횟수. 정상이면 0. */
  failures: number;
  /** 지금 상태(정상이면 복구 시각, 장애면 첫 실패 시각)가 시작된 때. */
  since: string;
  /** 장애 알림을 보냈는지. 보내지 못했으면 다음 분에 다시 보내요. */
  alerted: boolean;
  lastError: string;
}

// 한 번 실패는 폰 쪽처럼 순간 흔들림일 수 있어요. 2분 연속이면 알려요.
export const FAILURES_BEFORE_ALERT = 2;
const PROBE_TIMEOUT_MS = 10_000;
const STATE_KEY = "state";

// Cloudflare 가 origin 대신 돌려주는 상태 코드는 어디가 끊겼는지를 말해 줘요.
function explain(status: number): string {
  if (status === 530) return "터널 끊김(cloudflared 가 Cloudflare 에 붙어 있지 않음)";
  if (status === 502 || status === 504) return "Caddy 가 API 에 닿지 못함(컨테이너 멈춤·재시작 중)";
  if (status === 503) return "API 가 스스로 비정상이라고 답함";
  if (status === 404) return "/health 가 없음(옛 버전 배포)";
  return "예상 밖 응답";
}

export async function probe(url: string, fetcher: typeof fetch = fetch, timeoutMs = PROBE_TIMEOUT_MS): Promise<Probe> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetcher(url, { headers: { Accept: "application/json" }, signal: controller.signal });
    const body = (await response.json().catch(() => null)) as Record<string, unknown> | null;
    if (response.status === 200 && body?.ok === true) return { ok: true, detail: "ok" };
    const checks = body && typeof body === "object"
      ? Object.entries(body).filter(([key]) => key !== "ok").map(([key, value]) => `${key}=${String(value)}`).join(" ")
      : "";
    return { ok: false, detail: `HTTP ${response.status} ${explain(response.status)}${checks ? ` (${checks})` : ""}` };
  } catch (error) {
    if (controller.signal.aborted) return { ok: false, detail: `${timeoutMs / 1000}초 안에 응답 없음` };
    return { ok: false, detail: `연결 실패: ${error instanceof Error ? error.message : String(error)}` };
  } finally {
    clearTimeout(timer);
  }
}

function duration(from: string, to: Date): string {
  const minutes = Math.max(0, Math.round((to.getTime() - Date.parse(from)) / 60_000));
  if (minutes < 60) return `${minutes}분`;
  const hours = Math.floor(minutes / 60);
  return minutes % 60 ? `${hours}시간 ${minutes % 60}분` : `${hours}시간`;
}

const clock = (at: Date) =>
  at.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });

/** 이전 상태와 이번 결과로 다음 상태와(필요하면) 보낼 알림을 정해요. 부수 효과가 없어요. */
export function decide(previous: State | null, result: Probe, now: Date, url: string): { next: State; message: string | null } {
  const iso = now.toISOString();
  const prev: State = previous ?? { status: "up", failures: 0, since: iso, alerted: false, lastError: "" };

  if (result.ok) {
    const next: State = { status: "up", failures: 0, since: prev.status === "up" ? prev.since : iso, alerted: false, lastError: "" };
    const message = prev.status === "down" && prev.alerted
      ? `✅ 자리났다 서버 복구\n${clock(now)} · 장애 ${duration(prev.since, now)}\n마지막 원인: ${prev.lastError}`
      : null;
    return { next, message };
  }

  const failures = prev.failures + 1;
  const since = prev.status === "down" ? prev.since : iso;
  const alert = failures >= FAILURES_BEFORE_ALERT && !prev.alerted;
  const next: State = { status: "down", failures, since, alerted: prev.alerted, lastError: result.detail };
  const message = alert
    ? `🚨 자리났다 서버 장애\n${clock(new Date(since))}부터 ${failures}분 연속 실패\n원인: ${result.detail}\n${url}`
    : null;
  return { next, message };
}

export async function sendTelegram(env: Env, text: string, fetcher: typeof fetch = fetch): Promise<boolean> {
  try {
    const response = await fetcher(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text, disable_web_page_preview: true }),
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function readState(env: Env): Promise<State | null> {
  const raw = await env.STATE.get(STATE_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as State;
  } catch {
    return null;
  }
}

/** cron 한 번. KV 무료 한도(하루 쓰기 1,000회)보다 cron 이 잦아서, 상태가 바뀔 때만 써요. */
export async function check(env: Env, now = new Date(), fetcher: typeof fetch = fetch): Promise<State> {
  const previous = await readState(env);
  const result = await probe(env.HEALTH_URL, fetcher);
  const { next, message } = decide(previous, result, now, env.HEALTH_URL);
  if (message) {
    const sent = await sendTelegram(env, message, fetcher);
    // 장애 알림을 못 보냈으면 alerted 를 남기지 않아 다음 분에 다시 보내요. 복구 알림은 한 번만 시도해요.
    if (next.status === "down") next.alerted = sent;
  }
  if (JSON.stringify(next) !== JSON.stringify(previous)) await env.STATE.put(STATE_KEY, JSON.stringify(next));
  return next;
}
