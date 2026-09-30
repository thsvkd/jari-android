import { check, readState, type Env } from "./monitor";

export default {
  async scheduled(_controller: unknown, env: Env, ctx: { waitUntil(promise: Promise<unknown>): void }): Promise<void> {
    ctx.waitUntil(check(env));
  },

  // 지금 감시가 본 상태를 JSON 으로 보여 줘요(읽기 전용). 알림 없이 확인할 때 써요.
  async fetch(_request: Request, env: Env): Promise<Response> {
    const state = await readState(env);
    return Response.json(state ?? { status: "unknown" }, { headers: { "Cache-Control": "no-store" } });
  },
};
