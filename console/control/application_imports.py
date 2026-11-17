"""Durable explicit native imports. HTTP never supplies filesystem paths."""
import json
import sqlite3
import time
from pathlib import Path
from uuid import UUID

from chatcopilot.core.private_sqlite import private_lock
from studio import projects


class Imports:
    def __init__(self, applications):
        self.apps = applications
        self.studio = applications.studio()
        with self.studio.db.connect(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS studio_imports (id TEXT PRIMARY KEY, "
                       "state TEXT NOT NULL, selection TEXT, updated REAL NOT NULL)")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_native_import ON studio_imports((1)) "
                       "WHERE state IN ('pending','selecting','importing','recovery_required')")

    def _lock(self, key, timeout=0):
        return private_lock(self.apps.state_dir / ("live2d-import-" + key + ".lock"), timeout=timeout)

    def _row(self, key):
        with self.studio.db.connect() as db:
            row = db.execute("SELECT * FROM studio_imports WHERE id=?", (key,)).fetchone()
        if row is None:
            raise KeyError("导入请求不存在")
        return dict(row)

    def _update(self, key, state, selection=None):
        with self.studio.db.connect(write=True) as db:
            db.execute("UPDATE studio_imports SET state=?,selection=COALESCE(?,selection),updated=? WHERE id=?",
                       (state, selection, time.time(), key))

    def register(self, ident):
        key = UUID(str(ident)).hex
        with private_lock(self.apps.state_dir / ("project-create-" + key + ".lock"), timeout=1), self.studio.db.connect(write=True) as db:
            project = db.execute("SELECT bot FROM projects WHERE id=?", (key,)).fetchone()
            if project and project["bot"] != "live2d-desktop":
                raise ValueError("此请求标识已用于其他应用")
            existing = db.execute("SELECT id FROM studio_imports WHERE id=?", (key,)).fetchone()
            active = db.execute("SELECT id FROM studio_imports WHERE state IN "
                                "('pending','selecting','importing','recovery_required') LIMIT 1").fetchone()
            if not existing and active:
                key = active["id"]  # Show the active request instead of opening another native window.
            else:
                db.execute("INSERT INTO studio_imports(id,state,updated) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING",
                           (key, "imported" if project else "pending", time.time()))
        return self.status(key)

    def status(self, ident):
        key = UUID(str(ident)).hex
        row = self._row(key)
        state = row["state"]
        if state in {"selecting", "importing"}:
            try:
                with self._lock(key):
                    if self._completed(key, row):
                        self._update(key, "imported")
                        state = "imported"
                    else:
                        state = "recovery_required"
            except BlockingIOError:
                pass
        messages = {
            "pending": "等待导入；若刷新后未继续，请重试查询或以同一请求继续。",
            "selecting": "正在 Windows 中确认已安装的桌面伙伴；请完成选择或取消。",
            "importing": "正在校验并导入已选桌面伙伴的公开源码基线。",
            "imported": "已导入开发基线；不代表应用已启动或完成运行验收。",
            "cancelled": "已取消选择，未创建应用。",
            "failed": "导入失败。请核对已安装桌面伙伴的本机准备和载荷，再显式重试。",
            "recovery_required": "上次导入结果未知。已有来源固定复用；尚未记录来源时，必须确认旧 Windows 选择窗口已关闭才能重新选择。",
        }
        return {"id": key, "state": state, "application_id": "studio:" + key if state == "imported" else None,
                "message": messages[state], "retryable": state in {"cancelled", "failed", "recovery_required"},
                "requires_selection_closed": state == "recovery_required" and not row["selection"]}

    def retry(self, ident, *, selection_closed=False):
        key = UUID(str(ident)).hex
        try:
            with self._lock(key):
                row = self._row(key)
                if self._completed(key, row):
                    self._update(key, "imported")
                elif row["state"] in {"cancelled", "failed", "selecting", "importing", "recovery_required"}:
                    if row["state"] in {"selecting", "recovery_required"} and not row["selection"] and not selection_closed:
                        raise ValueError("请先确认旧 Windows 选择窗口已关闭；不能自动重复打开未知状态的选择器")
                    self._update(key, "pending")
        except BlockingIOError:
            pass
        return self.status(key)

    def _completed(self, key, row):
        if not row["selection"]:
            return False
        selected = json.loads(row["selection"])
        with self.studio.db.connect() as db:
            project = db.execute("SELECT bot,assets FROM projects WHERE id=?", (key,)).fetchone()
        if not project or project["bot"] != "live2d-desktop" or project["assets"] != selected["assets"]:
            return False
        from chatcopilot.core.source_snapshot import source_manifest, manifest_digest
        try:
            return manifest_digest(source_manifest(self.studio.directory(key) / "initial")) == selected["source_digest"]
        except (ValueError, OSError):
            return False

    @staticmethod
    def _selection(source, assets):
        from chatcopilot.deployment.application_bundle import read_bundle
        from chatcopilot.core.source_snapshot import manifest_digest
        package = read_bundle(assets, native_volume=True)
        return {"root": str(source), "assets": str(assets),
                "source_digest": manifest_digest(package["source_manifest"]),
                "artifacts_digest": package["artifacts_digest"]}

    def process(self, ident):
        key = UUID(str(ident)).hex
        try:
            with self._lock(key):
                row = self._row(key)
                if row["state"] != "pending":
                    return
                # One native selector at a time; duplicate request IDs never get
                # here while another process owns this request.
                with private_lock(self.apps.state_dir / "live2d-selector.lock", timeout=310):
                    try:
                        if row["selection"]:
                            selected = json.loads(row["selection"])
                            source, assets = Path(selected["root"]), Path(selected["assets"])
                        else:
                            self._update(key, "selecting")
                            source, assets = projects.select_live2d()
                            selected = self._selection(source, assets)
                            self._update(key, "importing", json.dumps(selected))
                        if self._selection(source, assets) != selected:
                            raise ValueError("所选安装版本已经变化，不能替换原导入来源")
                        self._update(key, "importing")
                        # Replaying after a crash reuses exactly the selected
                        # baseline. import_live2d verifies an existing initial.
                        if self.apps.get("studio:" + key) is not None:
                            if not self._completed(key, self._row(key)):
                                raise ValueError("已有项目与导入来源不一致")
                        else:
                            self.studio.create(key, live=True, selection=(source, assets))
                        self._update(key, "imported")
                    except projects.SelectionInterrupted:
                        self._update(key, "recovery_required")
                    except projects.SelectionCancelled:
                        self._update(key, "cancelled")
                    except (ValueError, OSError, sqlite3.Error, KeyError):
                        self._update(key, "failed")
        except (BlockingIOError, TimeoutError):
            return
