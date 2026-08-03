import { afterEach, describe, expect, it, vi } from "vitest";
import { streamTask } from "./api";

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
  globalThis.EventSource = originalEventSource;
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
