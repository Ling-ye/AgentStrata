import { describe, expect, it } from "vitest";
import { catalogLayer, deliveryLabel, inventoryUses, runState, type GatewayRunDetail } from "./model";
import type { BotInventory, CatalogItem } from "../../types";

describe("architecture evidence semantics", () => {
  it("does not promote a completed run to delivery evidence", () => {
    const detail = { run: { state: "completed" }, receipts: [] } as unknown as GatewayRunDetail;
    expect(deliveryLabel(detail)).toContain("不能证明消息送达");
    detail.receipts = [{ stage: "provider_acknowledged" }] as GatewayRunDetail["receipts"];
    expect(deliveryLabel(detail)).toContain("平台显示与用户已读尚无回执");
  });
  it("keeps recovery and unknown states distinct from success", () => {
    expect(runState("recovery_required").label).toBe("等待恢复");
    expect(runState("unexpected").color).toBe("gray");
  });
  it("keeps partial or unknown delivery visible alongside successful receipts", () => {
    const detail = { receipts: [{ stage: "provider_acknowledged" }], outbox: [{ state: "failed" }] } as GatewayRunDetail;
    expect(deliveryLabel(detail)).toContain("存在交付失败");
    detail.outbox = [{ state: "delivery_unknown" }] as GatewayRunDetail["outbox"];
    expect(deliveryLabel(detail)).toContain("存在交付结果未知");
  });
  it("resolves catalog prefixes against canonical inventory IDs and disabled MCP", () => {
    const inventory = { tool_packs: [{ id: "workspace.read_write" }], mcp_services: [{ ref: "browser", enabled: false }], agent_presets: [{ name: "research" }] } as BotInventory;
    expect(inventoryUses({ id: "tool_pack:workspace.read_write", kind: "tool_pack" } as CatalogItem, inventory)).toBe(true);
    expect(inventoryUses({ id: "sub:research", kind: "subagent" } as CatalogItem, inventory)).toBe(true);
    expect(inventoryUses({ id: "mcp:browser", kind: "mcp" } as CatalogItem, inventory)).toBe(false);
  });
  it("keeps capability components separate from application context", () => {
    expect(catalogLayer({ kind: "context_source" } as CatalogItem)).toBe("application");
    expect(catalogLayer({ kind: "mcp" } as CatalogItem)).toBe("capability");
  });
});
