import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Button, Space, Tag, Typography } from "@arco-design/web-react";
import { applicationHref } from "./navigation";
import { canStartNewImport, importStateLabel, live2dImportApi, live2dImportKey, readLive2DImport } from "./live2dImport";
const { Text } = Typography;
export default function Live2DImport() {
  const [id, setId] = useState<string>();
  const [restored, setRestored] = useState(false);
  const [storageError, setStorageError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const gate = useRef(false), alive = useRef(true);
  const client = useQueryClient();
  useEffect(() => {
    alive.current = true;
    try { setId(readLive2DImport(sessionStorage)); setRestored(true); }
    catch (error) { setStorageError(error instanceof Error ? error.message : "无法读取导入请求记录"); }
    return () => { alive.current = false; };
  }, []);
  const query = useQuery({ queryKey: ["live2d-import", id], queryFn: ({ signal }) => live2dImportApi.status(id!, signal), enabled: !!id, refetchInterval: query => {
    const state = query.state.data?.state;
    return state && ["imported", "cancelled", "failed", "recovery_required"].includes(state) ? false : 2000;
  }, retry: false });
  useEffect(() => {
    if (query.data?.state !== "imported" || !query.data.application_id) return;
    try {
      sessionStorage.removeItem(live2dImportKey);
      void client.invalidateQueries({ queryKey: ["applications"] });
      window.location.hash = applicationHref(query.data.application_id, "improve");
    } catch (error) { setStorageError(error instanceof Error ? error.message : "无法保存导入结果"); }
  }, [query.data, client]);
  async function operate(retry = false) {
    if (gate.current || !restored || storageError) return;
    const needsClosed = retry && query.data?.requires_selection_closed === true;
    const recovering = retry && query.data?.state === "recovery_required";
    if (!window.confirm(needsClosed ? "请先关闭上一次打开的 Windows 选择窗口。确认你已关闭旧窗口，并允许重新选择？未关闭时请取消，避免同时出现两个选择窗口。" : recovering ? "继续核对并导入上次已选来源？不会重新打开选择窗口，不会安装或启动应用。" : retry ? "重试原导入请求？已有来源保持不变，仅未记录来源时请求选择。未知窗口状态仍须先确认关闭；不安装或启动应用。" : "请求本机选择或识别已安装的 AgentStrata 桌面伙伴？可能弹出 Windows 选择窗口。只校验并导入公开源码基线，不复制账号密钥、不安装或启动，也不代表通过开发验收。")) return;
    gate.current = true; setBusy(true); setError("");
    const request = id || crypto.randomUUID().replace(/-/g, "");
    try {
      sessionStorage.setItem(live2dImportKey, request);
      setId(request);
      const result = retry ? await live2dImportApi.retry(request, needsClosed) : await live2dImportApi.start(request);
      // Another tab may already own an active import. Adopt its durable receipt.
      sessionStorage.setItem(live2dImportKey, result.id);
      client.setQueryData(["live2d-import", result.id], result);
      if (alive.current) setId(result.id);
    } catch (error) {
      if (alive.current) setError(error instanceof Error ? error.message : "导入结果未知，请核对原请求");
      void client.invalidateQueries({ queryKey: ["live2d-import", request] });
    } finally { gate.current = false; if (alive.current) setBusy(false); }
  }
  function reset() {
    if (gate.current || !canStartNewImport(query.data)) return;
    try { sessionStorage.removeItem(live2dImportKey); setId(undefined); setError(""); }
    catch (error) { setStorageError(error instanceof Error ? error.message : "无法清理已结束请求"); }
  }
  return <Space direction="vertical" size={16} style={{ width: "100%" }}>
    <Alert type="info" content="从已安装的 AgentStrata 桌面伙伴导入经过核对的公开源码基线。可能出现 Windows 选择窗口，请在本机完成选择；已有实例与账号配置保持不变。导入不是安装、启动或开发验收。" />
    {storageError && <Alert type="error" content={storageError} />}
    {id && <Text type="secondary">导入请求：{id}。刷新只核对这条请求，不会自动重新选择。</Text>}
    {query.data && <><Tag>{importStateLabel(query.data.state)}</Tag><Text>{query.data.message}</Text></>}
    {(error || query.isError) && <Alert type="warning" content={error || "暂时无法读取导入状态。结果未知时保留原编号，不要另外发起导入。"} />}
    <Space wrap>
      {!id && <Button type="primary" disabled={!restored || !!storageError} loading={busy} onClick={() => void operate()}>选择并导入桌面伙伴</Button>}
      {id && <Button disabled={busy} onClick={() => void query.refetch()}>核对原导入请求</Button>}
      {id && ((query.isError && !query.data) || query.data?.state === "pending") && <Button disabled={busy || !!storageError} onClick={() => void operate()}>继续同一导入请求</Button>}
      {id && query.data?.retryable && <Button disabled={busy || !!storageError} onClick={() => void operate(true)}>{query.data.requires_selection_closed ? "已关闭旧窗口，重新选择" : query.data.state === "recovery_required" ? "继续导入已选来源" : "重试原导入请求"}</Button>}
      {canStartNewImport(query.data) && <Button disabled={busy} onClick={reset}>结束此请求，重新选择</Button>}
    </Space>
    {query.data?.state === "recovery_required" && <Alert type="warning" content="上次选择或导入中断，不能当作成功。保留原请求与项目记录，按服务端提示核对；不要通过更换编号绕过恢复检查。" />}
  </Space>;
}
