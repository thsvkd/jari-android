import { afterEach, describe, expect, it, vi } from "vitest";

import {
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
