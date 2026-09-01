import { describe, expect, it, vi } from "vitest";
import { openNapcatLoginPage, safeNapcatLoginUrl } from "./napcatLogin";

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
});
