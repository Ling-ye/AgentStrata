import { describe, expect, it } from "vitest";
import { leavesBotInstance } from "./navigationGuard";

describe("bot configuration navigation boundary", () => {
  it.each(["#bots?instance=a&tab=tasks", "#bots?instance=a&tab=runtime", "#bots?instance=a&tab=configuration&layer=agent&entity=mcp:docs", "#bots"])("retains the draft for %s", (hash) => {
    expect(leavesBotInstance(hash, "a")).toBe(false);
  });
  it.each(["#bots?instance=b&tab=configuration", "#tools", "#overview", "#settings"])("guards leaving the instance for %s", (hash) => {
    expect(leavesBotInstance(hash, "a")).toBe(true);
  });
});
