import { useCallback, useEffect, useRef, useState } from "react";

export type StreamStatus = "connecting" | "live" | "reconnecting";

type StartStream = (
  onLine: (line: string) => void,
  onStatus: (status: StreamStatus) => void,
  onEnd: () => void,
) => () => void;

interface OpenOptions {
  title: string;
  running?: boolean;
  maxLines?: number;
}

export function useEventStreamLines(defaultMaxLines = 2000) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [running, setRunning] = useState(false);
  const closer = useRef<(() => void) | null>(null);

  useEffect(
    () => () => {
      closer.current?.();
      closer.current = null;
    },
    [],
  );

  const close = useCallback(() => {
    closer.current?.();
    closer.current = null;
    setOpen(false);
  }, []);

  const start = useCallback(
    (nextStream: StartStream, options: OpenOptions) => {
      closer.current?.();
      setTitle(options.title);
      setLines([]);
      setStatus("connecting");
      setRunning(options.running ?? false);
      setOpen(true);
      const maxLines = options.maxLines ?? defaultMaxLines;
      closer.current = nextStream(
        (line) => setLines((prev) => [...prev.slice(-(maxLines - 1)), line]),
        (nextStatus) => setStatus(nextStatus),
        () => setRunning(false),
      );
    },
    [defaultMaxLines],
  );

  return {
    open,
    title,
    lines,
    status,
    running,
    close,
    start,
  };
}
