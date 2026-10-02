import { describe, expect, it } from "vitest";
import { weixinErrorLabel, weixinLoginActive, weixinLoginLabels } from "./weixinModel";

describe("Weixin binding lifecycle", () => {
  it("polls QR and verification stages until a terminal result", () => {
    for (const state of ["creating", "wait", "scaned", "need_verifycode"]) expect(weixinLoginActive(state)).toBe(true);
    for (const state of ["confirmed", "expired", "cancelled", "failed", "save_failed", "unexpected"]) expect(weixinLoginActive(state)).toBe(false);
  });
  it("keeps saved credentials distinct from a connected runtime", () => {
    expect(weixinLoginLabels.confirmed).toBe("已绑定");
    expect(weixinLoginLabels.save_failed).toBe("保存配置失败");
  });
  it("explains account replacement and hides internal error details", () => {
    expect(weixinErrorLabel("weixin_account_replacement_requires_new_instance")).toContain("独立实例");
    expect(weixinErrorLabel("unexpected internal detail")).not.toContain("unexpected");
  });
});
