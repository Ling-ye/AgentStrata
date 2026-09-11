import { useState } from "react";
import { Button, Input, Message, Space, Typography } from "@arco-design/web-react";

export function InstanceId({ id, label }: { id: string; label: string }) {
  const [manual, setManual] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(id);
      setManual(false);
      Message.success(`${label}已复制`);
    } catch { setManual(true); }
  }
  return <Space direction="vertical" size={4} style={{ maxWidth: "100%" }}>
    <Typography.Text style={{ overflowWrap: "anywhere" }}>{label}：{id}</Typography.Text>
    <Button size="mini" aria-label={`复制${label} ${id}`} onClick={() => void copy()}>复制{label}</Button>
    {manual && <><Typography.Text type="secondary">自动复制不可用，请选中下方 ID 手动复制。</Typography.Text>
      <Input aria-label={`手动复制${label}`} readOnly value={id} onFocus={event => event.target.select()} /></>}
  </Space>;
}
