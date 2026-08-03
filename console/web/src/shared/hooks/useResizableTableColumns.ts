import { createElement, useCallback, useMemo, useState } from "react";
import { ResizableBox } from "react-resizable";
import type { ColumnProps, ResizableProps } from "../ui/arcoTypes";

type ColumnWidthMap<ColumnKey extends string> = Record<ColumnKey, number>;

interface ResizableTableColumnOptions<ColumnKey extends string> {
  defaultWidths: ColumnWidthMap<ColumnKey>;
  minWidths?: Partial<ColumnWidthMap<ColumnKey>>;
  minWidth?: number;
}

export function useResizableTableColumns<Row extends Record<string, any>, ColumnKey extends string>({
  defaultWidths,
  minWidths = {},
  minWidth = 80,
}: ResizableTableColumnOptions<ColumnKey>) {
  const [columnWidths, setColumnWidths] = useState<ColumnWidthMap<ColumnKey>>(() => ({
    ...defaultWidths,
  }));

  const tableWidth = useMemo(
    () =>
      (Object.keys(columnWidths) as ColumnKey[]).reduce(
        (total, key) => total + columnWidths[key],
        0,
      ),
    [columnWidths],
  );

  const resizable = useMemo<ResizableProps<Row>>(
    () => ({
      onResizeStop: (column) => {
        const resizedColumn = column as unknown as ColumnProps<Row>;
        const key = String(resizedColumn.dataIndex ?? resizedColumn.key ?? "");
        const width = Number(resizedColumn.width);
        if (key in defaultWidths && Number.isFinite(width)) {
          const columnKey = key as ColumnKey;
          setColumnWidths((prev) => ({
            ...prev,
            [columnKey]: Math.max(minWidths[columnKey] ?? minWidth, Math.round(width)),
          }));
        }
        return column;
      },
    }),
    [defaultWidths, minWidth, minWidths],
  );

  const withResizableColumns = useCallback(
    (columns: ColumnProps<Row>[]): ColumnProps<Row>[] =>
      columns.map((column) => {
        const width = Number(column.width);
        if (!Number.isFinite(width) || width <= 0) return column;
        return {
          ...column,
          title: createElement(
            ResizableBox,
            {
              axis: "x",
              width,
              height: 28,
              minConstraints: [60, 28],
              resizeHandles: ["e"],
              onResizeStop: (_, data) => {
                const nextColumn = { ...column, width: Math.round(data.size.width) };
                resizable.onResizeStop?.(nextColumn);
              },
            },
            createElement("div", { className: "cc-resizable-title" }, column.title),
          ),
        };
      }),
    [resizable],
  );

  return { columnWidths, resizable, tableWidth, withResizableColumns };
}
