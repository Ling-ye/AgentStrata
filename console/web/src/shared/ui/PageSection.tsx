import type { ReactNode } from "react";
import { Space, Typography } from "@arco-design/web-react";

const { Text, Title } = Typography;

interface PageSectionProps {
  title: ReactNode;
  description?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
}

export default function PageSection({ title, description, extra, children }: PageSectionProps) {
  return (
    <section className="console-section">
      <div className="section-header">
        <div className="section-heading">
          <Title heading={5} className="section-title">{title}</Title>
          {description && <Text type="secondary">{description}</Text>}
        </div>
        {extra && <Space wrap>{extra}</Space>}
      </div>
      {children}
    </section>
  );
}
