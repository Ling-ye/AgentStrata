import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Card, Input, Select, Space, Spin, Typography } from "@arco-design/web-react";
import { applicationsApi, installerUrl, type ApplicationView, type InstallPlatform } from "./api";
import { applicationHref } from "./navigation";
import DesktopLaunch from "./DesktopLaunch";
import ProjectTaskRecord from "./ProjectTaskRecord";

const names = { "windows-x64": "Windows x64", "linux-x64": "Linux x64" };
export function preferredPlatform(available: InstallPlatform[], agent: string) {
  return (agent.includes("Linux") && available.includes("linux-x64")) ? "linux-x64" : available[0];
}
export default function ApplicationInstallers({ application }: { application: ApplicationView }) {
  const id = application.id;
  const query = useQuery({
    queryKey: ["application-installers", id], queryFn: ({ signal }) => applicationsApi.installers(id, signal),
    retry: false, refetchInterval: q => q.state.data?.builds.some(b => ["building", "queued"].includes(b.state)) ? 2000 : false,
  });
  const [platform, setPlatform] = useState<InstallPlatform>();
  const [source, setSource] = useState<"commit" | "candidate">("commit");
  const [revision, setRevision] = useState<string>();
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const gate = useRef(false);
  const receiptInput = useRef<HTMLInputElement>(null);
  const data = query.data;
  if (!data) return query.isError ? <Alert type="error" content={String(query.error)} action={<Button onClick={() => void query.refetch()}>重试</Button>} /> : <Spin />;
  const target = platform ?? preferredPlatform(data.platforms, navigator.userAgent);
  const records = data.builds.filter(b => b.platform === target);
  const ready = records.filter(b => b.state === "ready");
  const current = ready.find(b => b.id === selected) ?? ready[0];
  const last = records[0];
  const value = (revision ?? (source === "commit" ? data.commit : "")).trim();
  const valid = source === "commit" ? /^[0-9a-f]{40}$/.test(value) : /^repair-[0-9a-f]{32}$/.test(value);
  const active = !!last && ["building", "queued"].includes(last.state);
  async function generate() {
    if (gate.current || !target || !valid) return;
    gate.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await applicationsApi.buildInstaller(id, target, source, value);
      setSelected(result.id);
      await query.refetch();
    } catch (failure) { setError(String(failure)); }
    finally { setBusy(false); gate.current = false; }
  }
  const docker = current?.docker ?? data.docker;
  const requirements = current?.requirements ?? data.requirements;
  return <Card title="下载与部署">
    <Space direction="vertical" className="studio-install">
      <dl className="studio-info">
        {target && <><dt>安装平台</dt><dd>{data.platforms.length > 1
          ? <Select aria-label="安装平台" value={target} options={data.platforms.map(p => ({ value: p, label: names[p] }))}
              onChange={p => { setPlatform(p); setSelected(""); setError(""); }} />
          : names[target]}</dd>
          <dt>运行要求</dt><dd>{docker ? (target === "windows-x64" ? "Docker Desktop（Linux 容器）及 Compose v2" : "Docker Engine 和 Compose v2") : "本机直接运行"}</dd></>}
        {requirements.length > 0 && <><dt>首次安装</dt><dd>{requirements.map(text => <p key={text}>{text}</p>)}</dd></>}
        {current && <><dt>安装版本</dt><dd>{ready.length > 1
          ? <Select aria-label="安装版本" value={current.id} onChange={setSelected}
              options={ready.map(b => ({ value: b.id, label: (b.origin.kind === "candidate" ? "已验收候选 " : "Git 提交 ") + (b.origin.revision || b.id).slice(0, 12) }))} />
          : (current.origin.kind === "candidate" ? "已验收候选 " : "Git 提交 ") + (current.origin.revision ?? current.id).slice(0,12)}</dd></>}
      </dl>
      {current && <Space><Button type="primary" href={installerUrl(id, current.id, target)}>下载安装包</Button>
        <Typography.Text type="secondary">{((current.size ?? 0) / 1048576).toFixed(1)} MB</Typography.Text></Space>}
      {current?.sha256 && <>
        <details><summary>包内组件</summary>
          {current.components?.length ? <ul>{current.components.map(item => <li key={item}>{item}</li>)}</ul>
            : <p>这次构建未记录组件明细，可用同一来源重新生成。</p>}
        </details>
        <details><summary>校验与运行记录</summary>
          <p>安装包 SHA-256</p><code>{current.sha256}</code>
          {current.origin.task && (application.source === "studio"
            ? <ProjectTaskRecord key={current.origin.task} id={id} task={current.origin.task} active={false} />
            : application.source === "release"
              ? <><p>独立验收任务 <code>{current.origin.task}</code></p>
                  {current.acceptance && <dl className="studio-info">
                    <dt>候选源码摘要</dt><dd><code>{current.acceptance.candidate_digest}</code></dd>
                    <dt>验证回执摘要</dt><dd><code>{current.acceptance.verification_digest}</code></dd>
                  </dl>}</>
              : <p><a href={applicationHref(id, "repairs", { task: current.origin.task })}>查看候选独立验收记录</a></p>)}
          <p>{current.installation
            ? "接收方安装器报告" + (current.installation.services_ready ? "已安装、服务就绪" : "已完成安装") + " · " + new Date(current.installation.completed_at * 1000).toLocaleString()
            : "尚无这个安装包的接收方安装记录。"}</p>
          {application.source !== "release" && <><input ref={receiptInput} type="file" accept=".json" hidden onChange={async event => {
            const file = event.currentTarget.files?.[0]; event.currentTarget.value = "";
            if (!file) return;
            setError("");
            try {
              await applicationsApi.importInstallation(id, current.id, target, JSON.parse((await file.text()).replace(/^\uFEFF/, "")));
              await query.refetch();
            } catch (failure) { setError(String(failure)); }
          }} />
          <Button size="small" onClick={() => receiptInput.current?.click()}>导入接收方安装记录</Button></>}
        </details>
      </>}
      {!current && !data.reason && <Typography.Text type="secondary">这个平台还没有安装包。</Typography.Text>}
      {data.reason && <Alert type="info" content={data.reason} action={<Button href={
        ["native", "plugin"].includes(application.source) || application.type === "companion" ? "#connect" : applicationHref(id, "improve")}>查看接入入口</Button>} />}
      {active && <Alert type="info" content={last.state === "queued" ? "等待构建。刷新或重新提交同一来源可以继续。" : "正在生成安装包，离开页面后仍会继续。"} />}
      {last?.state === "failed" && <Alert type="warning" content={last.error} />}
      {application.source !== "release" && !data.reason && target && <details open={!current || undefined} className="studio-build-options">
        <summary>生成安装包</summary>
        <Space direction="vertical">
          <label>构建来源<Select aria-label="构建来源" value={source} options={[
            {value: "commit", label: "指定 Git 提交"}, {value: "candidate", label: "已验收候选"},
          ]} onChange={kind => { setSource(kind); setRevision(undefined); }} /></label>
          <label>{source === "commit" ? "Git 提交 SHA" : "Harness 任务编号"}
            <Input value={revision ?? (source === "commit" ? data.commit : "")} onChange={setRevision}
              placeholder={source === "commit" ? "完整的 40 位提交 SHA" : "repair-…"} /></label>
          <Button type={current ? "secondary" : "primary"} disabled={!valid || last?.state === "building"} loading={busy}
            onClick={() => void generate()}>为 {names[target]} 生成安装包</Button>
        </Space>
      </details>}
      {error && <Alert type="error" content={error} />}
      {application.source === "studio" && ["desktop","companion"].includes(application.type) &&
        <details><summary>绑定本机已安装的程序</summary><DesktopLaunch id={id} /></details>}
    </Space>
  </Card>;
}
