import type { ColumnProps } from "@arco-design/web-react/es/Table/interface";

export type { ColumnProps };

export interface ResizableProps<Row> {
  onResizeStop?: (column: Partial<ColumnProps<Row>>) => Partial<ColumnProps<Row>>;
}
