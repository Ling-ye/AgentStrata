import { useMemo, useState } from "react";
import { Checkbox, Input, Modal, Space, Tag, Typography } from "@arco-design/web-react";
import { IconSearch } from "@arco-design/web-react/icon";
import type { CatalogItem } from "../types";

const { Text } = Typography;

interface Props {
  visible: boolean;
  title: string;
  items: CatalogItem[];
  selected: Set<string>;
  onConfirm: (added: string[]) => void;
  onCancel: () => void;
}

export default function ToolPickerModal({ visible, title, items, selected, onConfirm, onCancel }: Props) {
  const [search, setSearch] = useState("");
  const [checked, setChecked] = useState<Set<string>>(new Set());

  const available = useMemo(() => {
    let list = items.filter((i) => !selected.has(i.name));
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      list = list.filter(
        (i) =>
          i.name.toLowerCase().includes(q) ||
          i.description.toLowerCase().includes(q),
      );
    }
    return list;
  }, [items, selected, search]);

  const handleToggle = (name: string, on: boolean) => {
    setChecked((prev) => {
      const next = new Set(prev);
      if (on) next.add(name);
      else next.delete(name);
      return next;
    });
  };

  const handleOk = () => {
    onConfirm(Array.from(checked));
    setChecked(new Set());
    setSearch("");
  };

  const handleCancel = () => {
    onCancel();
    setChecked(new Set());
    setSearch("");
  };

  return (
    <Modal
      title={title}
      visible={visible}
      onOk={handleOk}
      onCancel={handleCancel}
      okText="添加"
      cancelText="取消"
      style={{ maxWidth: 560 }}
      okButtonProps={{ disabled: checked.size === 0 }}
    >
      <Input
        prefix={<IconSearch />}
        placeholder="搜索..."
        allowClear
        value={search}
        onChange={setSearch}
        style={{ marginBottom: 12 }}
      />

      <div style={{ maxHeight: 360, overflowY: "auto" }}>
        {available.length === 0 && (
          <Text type="secondary">没有更多可添加的项目</Text>
        )}
        {available.map((item) => (
          <div key={item.id} style={{ padding: "6px 0", borderBottom: "1px solid var(--color-border-1)" }}>
            <Space>
              <Checkbox
                checked={checked.has(item.name)}
                onChange={(on) => handleToggle(item.name, on)}
              />
              <div>
                <Space size={4} wrap>
                  <Text bold>{item.name}</Text>
                  <Tag size="small" color="gray" title={item.category}>{item.category}</Tag>
                  {item.risk && <Tag size="small" color="green" title={item.risk}>{item.risk}</Tag>}
                </Space>
                <div>
                  <Text type="secondary" className="cc-text-small">{item.description}</Text>
                </div>
              </div>
            </Space>
          </div>
        ))}
      </div>
    </Modal>
  );
}
