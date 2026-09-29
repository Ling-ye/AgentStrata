import { Alert, Message, Select, Space, Typography } from "@arco-design/web-react";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { llmApi, profileOptions } from "./api";

export default function ModelProfileSelect({ purpose, value = "", onChange, worker = false, disabled = false, persistBinding = false }:
  { purpose: string; value?: string; onChange?: (value: string) => void; worker?: boolean; disabled?: boolean; persistBinding?: boolean }) {
  const client = useQueryClient();
  const [saving, setSaving] = useState(false);
  const config = useQuery({ queryKey: ["llm-config"], queryFn: ({ signal }) => llmApi.config(signal), retry: false, refetchOnMount: "always" });
  const options = profileOptions(config.data, worker);
  const selected = value || config.data?.bindings[purpose];
  async function select(value: string) {
    if (!persistBinding) { onChange?.(value); return; }
    if (!config.data || !purpose || !value) return;
    setSaving(true);
    try {
      const saved = await llmApi.save({ ...config.data, bindings: { ...config.data.bindings, [purpose]: value } });
      client.setQueryData(["llm-config"], saved);
      await client.invalidateQueries();
      Message.success("方案引用已保存，请应用配置或重启机器人。");
    } catch (error) { Message.error(String(error)); }
    finally { setSaving(false); }
  }
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Select aria-label="模型配置方案" value={selected} loading={config.isPending || saving} disabled={disabled || config.isError || saving}
      allowClear={!purpose} placeholder={purpose ? "选择集中维护的模型方案" : "沿用机器人当前方案"} showSearch options={options} onChange={value => void select(value ?? "")} />
    {config.isError && <Alert type="error" content={String(config.error)} />}
    {!!purpose && !config.isPending && !config.isError && !options.some(option => option.value === selected) &&
      <Alert type="warning" content="此用途尚未绑定可用方案，请先完成模型配置。" />}
    <Typography.Text type="secondary">参数由模型配置页统一维护。<a href="#models">管理模型配置</a></Typography.Text>
  </Space>;
}
