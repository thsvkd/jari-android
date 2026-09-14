import { afterEach, describe, expect, it, vi } from "vitest";

import {
  awaitPushRegistration,
  clearToken,
  readToken,
  writeToken,
} from "./platform";

describe("browser session storage", () => {
  afterEach(async () => {
    await clearToken();
    vi.unstubAllGlobals();
  });

  it("keeps a browser token only in process memory", async () => {
    const persistedValues = new Map<string, string>();
    vi.stubGlobal("window", {
      localStorage: {
        getItem: (key: string) => persistedValues.get(key) ?? null,
        setItem: (key: string, value: string) => persistedValues.set(key, value),
      },
      sessionStorage: {
        getItem: (key: string) => persistedValues.get(key) ?? null,
        setItem: (key: string, value: string) => persistedValues.set(key, value),
      },
    });
    await writeToken("session-token");

    await expect(readToken()).resolves.toBe("session-token");
    expect(window.localStorage.getItem("teum.session")).toBeNull();
    expect(window.sessionStorage.getItem("teum.session")).toBeNull();
  });

  it("removes the browser token when cleared", async () => {
    await writeToken("session-token");
    await clearToken();

    await expect(readToken()).resolves.toBeNull();
  });
});

describe("push registration", () => {
  it("waits until the token has been saved before completing", async () => {
    let finishSaving: (saved: boolean) => void = () => undefined;
    const saved = new Promise<boolean>((resolve) => {
      finishSaving = resolve;
    });
    let completed = false;
    const registration = awaitPushRegistration(async () => undefined, saved).then((result) => {
      completed = true;
      return result;
    });

    await Promise.resolve();
    expect(completed).toBe(false);

    finishSaving(true);
    await expect(registration).resolves.toBe(true);
  });
});
