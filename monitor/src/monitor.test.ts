import { describe, expect, it, vi } from "vitest";

import { check, decide, probe, type Env, type State } from "./monitor";

const URL = "https://jari.example/api/mobile/health";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function memoryEnv() {
  const store = new Map<string, string>();
  const put = vi.fn(async (key: string, value: string) => void store.set(key, value));
  const env: Env = { STATE: { get: async (key) => store.get(key) ?? null, put }, HEALTH_URL: URL, TELEGRAM_BOT_TOKEN: "bot-token", TELEGRAM_CHAT_ID: "42" };
  return { env, put };
}

/** /health 는 answers 를 차례로 돌려주고, 텔레그램은 sent 에 모아요. */
function network(answers: Array<Response | Error>, telegramOk = true) {
  const sent: string[] = [];
  const fetcher = vi.fn<typeof fetch>(async (input, init) => {
    if (String(input).startsWith("https://api.telegram.org/")) {
      sent.push(JSON.parse(String(init!.body)).text as string);
      return new Response("{}", { status: telegramOk ? 200 : 500 });
    }
    const answer = answers.shift()!;
    if (answer instanceof Error) throw answer;
    return answer;
  });
  return { fetcher, sent };
}

const minute = (n: number) => new Date(Date.UTC(2026, 8, 30, 3, n));

describe("probe", () => {
  it("is healthy only on 200 with ok:true", async () => {
    expect(await probe(URL, network([json({ ok: true, redis: true, lease: true })]).fetcher)).toEqual({ ok: true, detail: "ok" });
    expect(await probe(URL, network([json({ ok: false, redis: false, lease: true }, 503)]).fetcher)).toEqual({
      ok: false,
      detail: "HTTP 503 API 가 스스로 비정상이라고 답함 (redis=false lease=true)",
    });
  });

  it("names where the path broke from Cloudflare's status", async () => {
    expect((await probe(URL, network([new Response("", { status: 530 })]).fetcher)).detail).toContain("터널 끊김");
    expect((await probe(URL, network([new Response("", { status: 502 })]).fetcher)).detail).toContain("Caddy 가 API 에 닿지 못함");
    expect((await probe(URL, network([new TypeError("fetch failed")]).fetcher)).detail).toBe("연결 실패: fetch failed");
  });

  it("gives up after the timeout", async () => {
    vi.useFakeTimers();
    try {
      const hanging = vi.fn<typeof fetch>((_, init) => new Promise((_, reject) => {
        init!.signal!.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }));
      const result = probe(URL, hanging, 10_000);
      await vi.advanceTimersByTimeAsync(10_000);
      expect(await result).toEqual({ ok: false, detail: "10초 안에 응답 없음" });
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("decide", () => {
  const down = { ok: false, detail: "HTTP 530 터널 끊김" };
  const up = { ok: true, detail: "ok" };

  it("alerts once on the second failure in a row, not on a single blip", () => {
    const first = decide(null, down, minute(0), URL);
    expect(first.message).toBeNull();
    const blip = decide(first.next, up, minute(1), URL);
    expect(blip.message).toBeNull();
    expect(blip.next).toMatchObject({ status: "up", failures: 0 });

    const again = decide(blip.next, down, minute(2), URL);
    const second = decide(again.next, down, minute(3), URL);
    expect(second.message).toContain("🚨 자리났다 서버 장애");
    expect(second.message).toContain("2분 연속 실패");
    expect(second.message).toContain("원인: HTTP 530 터널 끊김");
    const third = decide({ ...second.next, alerted: true }, down, minute(4), URL);
    expect(third.message).toBeNull();
  });

  it("reports recovery with how long it was down, counted from the first failure", () => {
    const alerted: State = { status: "down", failures: 5, since: minute(0).toISOString(), alerted: true, lastError: "HTTP 502" };
    const { next, message } = decide(alerted, up, minute(75), URL);
    expect(message).toContain("✅ 자리났다 서버 복구");
    expect(message).toContain("장애 1시간 15분");
    expect(message).toContain("마지막 원인: HTTP 502");
    expect(next).toEqual({ status: "up", failures: 0, since: minute(75).toISOString(), alerted: false, lastError: "" });
  });
});

describe("check", () => {
  it("writes state only when it changes, well inside KV's free 1,000 writes a day", async () => {
    const { env, put } = memoryEnv();
    const { fetcher } = network([json({ ok: true }), json({ ok: true }), json({ ok: true })]);
    await check(env, minute(0), fetcher);
    await check(env, minute(1), fetcher);
    await check(env, minute(2), fetcher);
    expect(put).toHaveBeenCalledTimes(1);
  });

  it("sends the outage alert, then the recovery", async () => {
    const { env } = memoryEnv();
    const { fetcher, sent } = network([new Response("", { status: 530 }), new Response("", { status: 530 }), new Response("", { status: 530 }), json({ ok: true })]);
    for (let n = 0; n < 4; n += 1) await check(env, minute(n), fetcher);
    expect(sent).toHaveLength(2);
    expect(sent[0]).toContain("🚨");
    expect(sent[1]).toContain("✅");
    expect(sent[1]).toContain("장애 3분");
  });

  it("tries the outage alert again next minute when Telegram did not take it", async () => {
    const { env } = memoryEnv();
    const failing = network([new Response("", { status: 530 }), new Response("", { status: 530 })], false);
    await check(env, minute(0), failing.fetcher);
    const state = await check(env, minute(1), failing.fetcher);
    expect(failing.sent).toHaveLength(1);
    expect(state.alerted).toBe(false);
    const working = network([new Response("", { status: 530 })]);
    expect((await check(env, minute(2), working.fetcher)).alerted).toBe(true);
    expect(working.sent).toHaveLength(1);
  });
});
