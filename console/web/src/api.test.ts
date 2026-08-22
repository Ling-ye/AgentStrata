import { afterEach, describe, expect, it, vi } from "vitest";
import { api, streamTask } from "./api";

class MockEventSource {
  static instances: MockEventSource[] = [];

  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  readonly close = vi.fn();
  private readonly listeners = new Map<string, Array<() => void>>();

  constructor(url: string | URL) {
    this.url = String(url);
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: () => void) {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  emit(type: string) {
    for (const listener of this.listeners.get(type) ?? []) listener();
  }
}

const originalEventSource = globalThis.EventSource;

afterEach(() => {
  MockEventSource.instances = [];
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  globalThis.EventSource = originalEventSource;
});

describe("task flow API", () => {
  it("uses the instance-scoped encoded flow endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ schema_version: 1, transitions: [] }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.taskFlow("qq-bot", "task/with space");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/bots/qq-bot/tasks/task%2Fwith%20space/flow",
      undefined,
    );
  });

  it("deletes an encoded instance-scoped task record", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, deleted: true, task_id: "task/with space" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await api.deleteTask("qq-bot", "task/with space");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/bots/qq-bot/tasks/task%2Fwith%20space",
      { method: "DELETE" },
    );
  });
});

describe("streamTask", () => {
  it("only treats the custom end event as terminal and lets EventSource reconnect", () => {
    globalThis.EventSource = MockEventSource as unknown as typeof EventSource;
    const onEnd = vi.fn();
    const onStatus = vi.fn();

    streamTask("task_1", vi.fn(), onEnd, onStatus);

    const source = MockEventSource.instances[0];
    expect(source.url).toBe("/api/tasks/task_1/stream");
    expect(onStatus).toHaveBeenLastCalledWith("connecting");

    source.onerror?.();
    expect(onStatus).toHaveBeenLastCalledWith("reconnecting");
    expect(source.close).not.toHaveBeenCalled();
    expect(onEnd).not.toHaveBeenCalled();

    source.onopen?.();
    expect(onStatus).toHaveBeenLastCalledWith("live");

    source.emit("end");
    expect(source.close).toHaveBeenCalledOnce();
    expect(onEnd).toHaveBeenCalledOnce();
  });
});
