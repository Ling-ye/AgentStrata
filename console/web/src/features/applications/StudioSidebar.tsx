import { useState } from "react";
import { IconMenuFold, IconMenuUnfold } from "@arco-design/web-react/icon";
import type { ReactNode } from "react";

export interface StudioGroup {
  title: string;
  items: { label: string; href: string }[];
}
export function StudioShell({
  groups,
  current,
  children,
}: {
  groups: StudioGroup[];
  current: string;
  children: ReactNode;
}) {
  const [mini, setMini] = useState(false);
  return (
    <div className="studio-workspace" data-mini={mini}>
      <nav className="studio-side" aria-label="工作区栏目">
        <select
          className="studio-mobile-menu"
          aria-label="切换栏目"
          value={current}
          onChange={e => {
            window.location.hash = e.target.value;
          }}
        >
          {groups.map(g => (
            <optgroup key={g.title} label={g.title}>
              {g.items.map(i => (
                <option key={i.href} value={i.href}>
                  {i.label}
                </option>
              ))}
            </optgroup>
          ))}
        </select>
        {groups.map(g => (
          <section key={g.title}>
            <div className="studio-group-label">{g.title}</div>
            {g.items.map(i => (
              <a
                key={i.href}
                href={i.href}
                aria-label={i.label}
                title={mini ? i.label : undefined}
                aria-current={current === i.href ? "page" : undefined}
              >
                {mini ? i.label.slice(0, 2) : i.label}
              </a>
            ))}
          </section>
        ))}
        <button
          type="button"
          className="studio-fold"
          onClick={() => setMini(!mini)}
          aria-label={mini ? "展开侧栏" : "收起侧栏"}
        >
          {mini ? <IconMenuUnfold /> : <IconMenuFold />}
        </button>
      </nav>
      <main className="studio-main">{children}</main>
    </div>
  );
}
