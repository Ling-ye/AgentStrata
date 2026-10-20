import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Modal, Spin, Typography } from "@arco-design/web-react";

interface LegalInfoResponse {
  project: string;
  copyright: string;
  license: string;
  repository_url: string;
  source_url: string;
  source_configured: boolean;
  license_url: string;
  notices_url: string;
  warranty: string;
}

export default function LegalInfo() {
  const [open, setOpen] = useState(false);
  const query = useQuery<LegalInfoResponse>({
    queryKey: ["legal"],
    queryFn: async () => {
      const response = await fetch("/api/legal");
      if (!response.ok) throw new Error("许可信息读取失败");
      return response.json();
    },
    enabled: open,
  });
  const info = query.data;
  return (
    <div className="console-legal">
      <Button type="text" size="small" onClick={() => setOpen(true)}>许可与源码</Button>
      <Modal title="许可与源码" visible={open} onCancel={() => setOpen(false)} footer={null}
        style={{ width: "min(560px, calc(100vw - 32px))" }}>
        {query.isPending && <Spin tip="正在读取许可信息" />}
        {query.isError && <Alert type="error" content={<>
          许可信息读取失败。<Button type="text" onClick={() => void query.refetch()}>重试</Button>
        </>} />}
        {info && <div className="console-legal-content">
          <Typography.Title heading={6}>{info.project} · {info.license}</Typography.Title>
          <Typography.Paragraph>{info.copyright}</Typography.Paragraph>
          <Typography.Paragraph>{info.warranty}</Typography.Paragraph>
          {!info.source_configured && <Alert type="warning" content="尚未设置本部署对应源码，以下地址仅为项目仓库。" />}
          <Typography.Paragraph>
            <a href={info.source_url} target="_blank" rel="noopener noreferrer">
              {info.source_configured ? "本部署对应源码" : "项目仓库"}：{info.source_url}
            </a>
          </Typography.Paragraph>
          <Typography.Paragraph>
            <a href={info.license_url} target="_blank" rel="noopener noreferrer">许可全文</a>
            {" · "}<a href={info.notices_url} target="_blank" rel="noopener noreferrer">第三方声明</a>
            {" · "}<a href="/third-party-notices.txt" target="_blank" rel="noopener noreferrer">本构建附带的许可文本</a>
          </Typography.Paragraph>
        </div>}
      </Modal>
    </div>
  );
}
