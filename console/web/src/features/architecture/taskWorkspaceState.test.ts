import { describe, expect, it } from "vitest";
import { botTabFromParams, scrollBookmark, taskWorkspaceState } from "./taskWorkspaceState";

describe("task workspace navigation", () => {
  it.each(["observation", "history", "tasks"])("routes %s to the task workspace without losing link parameters", (tab) => {
    const params = new URLSearchParams({ tab, instance: "bot-a", run: "task-17" });
    expect(botTabFromParams(params)).toBe("tasks");
    expect(taskWorkspaceState("bot-a", params, { selected: "older-task" }).selected).toBe("task-17");
    expect(params.get("instance")).toBe("bot-a");
    expect(params.get("run")).toBe("task-17");
  });
  it("defaults to tasks and keeps direct component links in configuration", () => {
    expect(botTabFromParams(new URLSearchParams())).toBe("tasks");
    expect(botTabFromParams(new URLSearchParams("entity=tool:lookup"))).toBe("configuration");
    expect(botTabFromParams(new URLSearchParams("tab=runtime"))).toBe("runtime");
    expect(botTabFromParams(new URLSearchParams("tab=capabilities"))).toBe("capabilities");
  });
  it("restores the instance selection instead of importing another instance's task", () => {
    const saved = { selected: "bot-b-task", filters: { page: 2, state: "failed" } };
    const result = taskWorkspaceState("bot-b", new URLSearchParams("instance=bot-a&run=bot-a-task"), saved);
    expect(result.selected).toBe("bot-b-task");
    expect(result.filters).toMatchObject({ page: 2, state: "failed" });
    expect(taskWorkspaceState("bot-c", new URLSearchParams("instance=bot-a&run=bot-a-task"), null).selected).toBe("");
  });
});

describe("restoring persisted task state", () => {
  it("refreshes rolling time windows while keeping task, page and search", () => {
    const result = taskWorkspaceState("a", new URLSearchParams(), {
      selected: "outside-current-page", range: "7", filters: { since: 1, until: 2, page: 2, search: "lookup" },
    }, 1_000_000);
    expect(result.selected).toBe("outside-current-page");
    expect(result.filters).toEqual({ page: 2, search: "lookup", since: 1_000_000 - 7 * 86400 });
  });
  it("keeps explicit historical date ranges", () => {
    const result = taskWorkspaceState("a", new URLSearchParams(), {
      range: "custom", filters: { since: 10, until: 20 }, customStart: "2026-09-01T00:00", customEnd: "2026-09-02T00:00",
    });
    expect(result.filters).toEqual({ since: 10, until: 20, page: 1 });
    expect(result.customStart).toBe("2026-09-01T00:00");
  });
  it("rejects malformed state and does not restore persisted task bodies as query fields", () => {
    const result = taskWorkspaceState("a", new URLSearchParams(), {
      range: "invalid", selected: {}, filters: { page: -4, min_ms: Infinity, model: {}, input: "private body", configuration: {} },
    }, 100_000);
    expect(result.filters).toEqual({ page: 1, since: 13_600 });
    expect(result.selected).toBe("");
    expect(result.range).toBe("1");
    expect(taskWorkspaceState("a", new URLSearchParams(), null).filters.page).toBe(1);
  });
  it("keeps a partially visible reading anchor and sanitizes corrupt bookmarks", () => {
    expect(scrollBookmark({ top: 3500, anchor: "trace:span", offset: -100, pages: 3 })).toEqual({ top: 3500, anchor: "trace:span", offset: -100, pages: 3 });
    expect(scrollBookmark({ top: -20, offset: NaN, pages: Infinity, anchor: {} })).toEqual({ top: 0, offset: 0, pages: 1, anchor: undefined });
  });
});
