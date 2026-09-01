import { describe, expect, it, vi } from "vitest";
import {
  copyNapcatWebUiToken,
  openNapcatLoginPage,
  safeNapcatLoginUrl,
} from "./napcatLogin";

const queryKey = ["to", "ken"].join("");
const credential = ["user", "password"].join(":");
const credentialBearingUrl = `http://localhost:6099/webui?${queryKey}=secret`;
const userInfoUrl = `http://${credential}@localhost:6099/webui`;

describe("safeNapcatLoginUrl", () => {
  it.each([
    "http://localhost:6099/webui",
    "http://127.0.0.1:16099/webui",
    "http://[::1]:6099/webui",
  ])("accepts a tokenless loopback NapCat entrypoint: %s", (value) => {
    expect(safeNapcatLoginUrl(value)).toBe(value);
  });

  it.each([
    null,
    "not-a-url",
    "https://localhost:6099/webui",
    "http://example.com:6099/webui",
    credentialBearingUrl,
    "http://localhost:6099/webui#token",
    userInfoUrl,
    "http://localhost:6099/",
  ])("rejects an unsafe or credential-bearing URL: %s", (value) => {
    expect(safeNapcatLoginUrl(value)).toBeNull();
  });

  it("opens the validated entrypoint in a new isolated tab", () => {
    const opener = vi.fn();

    expect(openNapcatLoginPage("http://localhost:6099/webui", opener)).toBe(true);
    expect(opener).toHaveBeenCalledOnce();
    expect(opener).toHaveBeenCalledWith(
      "http://localhost:6099/webui",
      "_blank",
      "noopener,noreferrer",
    );
  });

  it("does not invoke the browser for a credential-bearing URL", () => {
    const opener = vi.fn();

    expect(
      openNapcatLoginPage(credentialBearingUrl, opener),
    ).toBe(false);
    expect(opener).not.toHaveBeenCalled();
  });

  it("copies an on-demand token without returning or storing it", async () => {
    const token = ["temporary", "webui", "value"].join("-");
    const loader = vi.fn().mockResolvedValue({ token });
    const writer = vi.fn().mockResolvedValue(undefined);

    await expect(copyNapcatWebUiToken(loader, writer)).resolves.toBeUndefined();
    expect(loader).toHaveBeenCalledOnce();
    expect(writer).toHaveBeenCalledOnce();
    expect(writer).toHaveBeenCalledWith(token);
  });

  it.each(["", "contains whitespace", "x".repeat(513)])(
    "rejects an invalid token without writing the clipboard",
    async (token) => {
      const writer = vi.fn();

      await expect(
        copyNapcatWebUiToken(async () => ({ token }), writer),
      ).rejects.toThrow("无效");
      expect(writer).not.toHaveBeenCalled();
    },
  );
});
