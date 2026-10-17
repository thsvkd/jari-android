import { afterEach, expect, it, vi } from "vitest";
import { JariApp } from "./app";
import { createDemoApi } from "./demo";

const PAYMENT_URL = "https://www.korail.com/ticket/reservation/list";
const mounted: JariApp[] = [];
afterEach(() => { mounted.splice(0).forEach((app) => app.dispose()); document.body.replaceChildren(); window.localStorage.clear(); });

async function mountWithPending(options: { demoMode: boolean; onOpenKorailPayment?: (webUrl: string) => Promise<"app" | "web"> }, paymentUrl = PAYMENT_URL) {
  const demo = createDemoApi();
  const root = document.createElement("div"); document.body.append(root);
  const app = new JariApp(root, { ...demo,
    bootstrap: async () => ({
      ...(await demo.bootstrap()),
      pending: [{ reservationId: "320261017000001", trainInfo: "KTX 101 서울→부산", expiresAt: new Date(Date.now() + 600_000).toISOString(), seatNumber: "5A", seatClass: "general" as const }],
      paymentUrl,
    }),
  }, options);
  mounted.push(app);
  await app.start(true);
  await vi.waitFor(() => expect(root.querySelector(".payment-card a[data-korail-pay]")).not.toBeNull());
  return root.querySelector<HTMLAnchorElement>(".payment-card a[data-korail-pay]")!;
}

function clickLink(link: HTMLAnchorElement): MouseEvent {
  const click = new MouseEvent("click", { bubbles: true, cancelable: true });
  link.dispatchEvent(click);
  return click;
}

it("hands the payment link to the Korail app opener on Android instead of following it", async () => {
  const onOpenKorailPayment = vi.fn(async () => "app" as const);
  const link = await mountWithPending({ demoMode: false, onOpenKorailPayment });
  expect(link.getAttribute("href")).toBe(PAYMENT_URL);

  expect(clickLink(link).defaultPrevented).toBe(true);
  expect(onOpenKorailPayment).toHaveBeenCalledWith(PAYMENT_URL);
});

it("opens the Korail app once however fast the button is pressed again", async () => {
  let finish: (opened: "app") => void = () => undefined;
  const onOpenKorailPayment = vi.fn(() => new Promise<"app">((resolve) => { finish = resolve; }));
  const link = await mountWithPending({ demoMode: false, onOpenKorailPayment });
  clickLink(link);
  clickLink(link);
  expect(onOpenKorailPayment).toHaveBeenCalledTimes(1);
  finish("app");
  await vi.waitFor(() => { clickLink(link); expect(onOpenKorailPayment).toHaveBeenCalledTimes(2); });
});

it("does not hand over a payment address that is not https", async () => {
  const onOpenKorailPayment = vi.fn(async () => "web" as const);
  const link = await mountWithPending({ demoMode: false, onOpenKorailPayment }, "javascript:alert(1)");
  expect(link.getAttribute("href")).toBe("#");
  clickLink(link);
  expect(onOpenKorailPayment).not.toHaveBeenCalled();
});

it("says so when neither the Korail app nor a browser can open the payment", async () => {
  const link = await mountWithPending({ demoMode: false, onOpenKorailPayment: async () => { throw new Error("코레일 결제 화면을 열 앱이 없어요."); } });
  clickLink(link);
  await vi.waitFor(() => expect(document.body.textContent).toContain("코레일 결제 화면을 열 앱이 없어요."));
});

it("leaves the plain web link alone in the browser build", async () => {
  const link = await mountWithPending({ demoMode: false });
  expect(clickLink(link).defaultPrevented).toBe(false);
});

it("leaves the plain web link alone in the demo, which must not open the real Korail app", async () => {
  const onOpenKorailPayment = vi.fn(async () => "app" as const);
  const link = await mountWithPending({ demoMode: true, onOpenKorailPayment });
  expect(clickLink(link).defaultPrevented).toBe(false);
  expect(onOpenKorailPayment).not.toHaveBeenCalled();
});
