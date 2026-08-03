import { useEffect, useRef } from "react";

interface Props {
  lines: string[];
}

function lineClass(line: string): string {
  if (/\[ERR\]|level=ERROR|panic:|失败/.test(line)) return "log-line-err";
  if (/\[OK\]|完成|connected to wss/.test(line)) return "log-line-ok";
  if (/^\[console\]/.test(line)) return "log-line-console";
  return "";
}

export default function LogConsole({ lines }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  useEffect(() => {
    if (stickRef.current && ref.current) {
      ref.current.scrollTop = ref.current.scrollHeight;
    }
  }, [lines]);

  return (
    <div className="log-view" ref={ref} onScroll={onScroll}>
      {lines.length === 0 ? (
        <span style={{ opacity: 0.5 }}>（暂无输出）</span>
      ) : (
        lines.map((l, i) => (
          <div key={i} className={lineClass(l)}>
            {l || "\u00a0"}
          </div>
        ))
      )}
    </div>
  );
}
