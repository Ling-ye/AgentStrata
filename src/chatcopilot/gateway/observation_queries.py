"""Indexed history, evidence and metric queries for the observation workbench."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any

from .observation_store import ObservationStore, RETENTION_SECONDS, checked_run_id, decoded


@dataclass(frozen=True)
class RunFilter:
    since: float | None = None
    until: float | None = None
    state: str = ""
    config_id: str = ""
    backend: str = ""
    model: str = ""
    component: str = ""
    error_code: str = ""
    search: str = ""
    min_ms: float | None = None
    page: int = 1
    limit: int = 50

    def clause(self) -> tuple[str, list[Any]]:
        if not 1 <= self.limit <= 100 or not 1 <= self.page <= 100000:
            raise ValueError("Invalid observation page")
        if any(value is not None and (not math.isfinite(value) or value < 0)
               for value in (self.since, self.until, self.min_ms)):
            raise ValueError("Invalid observation time range")
        if self.since is not None and self.until is not None and self.since > self.until:
            raise ValueError("Invalid observation time range")
        clauses, params = [], []
        if any(len(getattr(self, name)) > 256 for name in ("state", "backend", "config_id", "model", "component", "error_code", "search")):
            raise ValueError("Observation filter is too long")
        for name, value, operator in (("created_at", self.since, ">="), ("created_at", self.until, "<=")):
            if value is not None:
                clauses.append(f"r.{name}{operator}?")
                params.append(value)
        for name in ("state", "backend"):
            value = getattr(self, name)
            if len(value) > 256:
                raise ValueError("Observation filter is too long")
            if value:
                clauses.append(f"r.{name}=?")
                params.append(value)
        if self.config_id:
            clauses.append("(r.config_revision=? OR r.config_id=?)")
            params.extend([self.config_id, self.config_id])
        if self.error_code:
            clauses.append("(r.error_code=? OR EXISTS(SELECT 1 FROM events e WHERE e.run_id=r.run_id AND e.error_code=?))")
            params.extend([self.error_code, self.error_code])
        if self.model:
            clauses.append("(r.model=? OR EXISTS(SELECT 1 FROM events e WHERE e.run_id=r.run_id AND e.model=?))")
            params.extend([self.model, self.model])
        if self.component:
            clauses.append("EXISTS(SELECT 1 FROM events e WHERE e.run_id=r.run_id AND "
                           "(e.entity_id=? OR EXISTS(SELECT 1 FROM json_each(e.refs) WHERE value=?)))")
            params.extend([self.component, self.component])
        if self.search:
            if len(self.search) > 256:
                raise ValueError("Observation search is too long")
            clauses.append("instr(lower(r.run_id),lower(?))>0")
            params.append(self.search)
        if self.min_ms is not None:
            clauses.append("(COALESCE(r.finished_at,?)-r.started_at)*1000>=?")
            params.extend([time.time(), self.min_ms])
        return " AND ".join(clauses) or "1=1", params


def _run(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key in ("receipts", "outbox", "approvals"):
        if key in result:
            result[key] = decoded(result[key])
    result["details_expires_at"] = result["finished_at"] + RETENTION_SECONDS if result.get("finished_at") is not None else None
    if result["details_expires_at"] is not None and result["details_expires_at"] <= time.time():
        result["details_expired"] = True
    return result


def history(store: ObservationStore, filters: RunFilter) -> dict[str, Any]:
    clause, params = filters.clause()
    with store.connection() as connection:
        rows = connection.execute("SELECT r.*, "
            "(SELECT COUNT(*) FROM events e WHERE e.run_id=r.run_id AND e.kind='LlmCallFinished') AS model_calls, "
            "(SELECT COUNT(*) FROM events e WHERE e.run_id=r.run_id AND e.kind='ToolStarted') AS tool_calls, "
            "(SELECT SUM(total_tokens) FROM events e WHERE e.run_id=r.run_id AND e.kind='LlmCallFinished') AS total_tokens "
            f"FROM runs r WHERE {clause} ORDER BY r.created_at DESC,r.run_id DESC LIMIT ? OFFSET ?",
            [*params, filters.limit, (filters.page - 1) * filters.limit]).fetchall()
        summary = dict(connection.execute(
            "SELECT COUNT(*) AS total,SUM(state IN ('accepted','running','abort_requested','recovery_required')) AS active,"
            "SUM(state='failed') AS failed_recent FROM runs r WHERE " + clause, params).fetchone())
    audit = store.meta("instance_audit") or {"audit": [], "audit_truncated": False}
    return {"runs": [_run(row) for row in rows], "page": filters.page, "limit": filters.limit,
            "total": summary["total"], "has_more": summary["total"] > filters.page * filters.limit,
            "summary": summary, **audit, "truncated": False,
            "source": "observation_index", "generated_at": time.time()}


def events(store: ObservationStore, run_id: str, *, after: int = 0, limit: int = 200) -> dict[str, Any]:
    checked_run_id(run_id)
    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("Invalid event cursor")
    with store.connection() as connection:
        rows = connection.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                                  (run_id, after, limit + 1)).fetchall()
    items = []
    for row in rows[:limit]:
        item = dict(row)
        item["data"] = decoded(item.pop("metadata"))
        item["refs"] = decoded(item["refs"])
        items.append(item)
    return {"source": "observation_index", "run_id": run_id, "observations": items, "next_cursor": items[-1]["seq"] if items else after,
            "has_more": len(rows) > limit, "generated_at": time.time()}


def detail(store: ObservationStore, run_id: str) -> dict[str, Any] | None:
    checked_run_id(run_id)
    with store.connection() as connection:
        row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    run = _run(row)
    run["trace"] = store.meta("trace:" + run_id) or {"capture_state": "not_recorded"}
    event_page = events(store, run_id)
    return {"run": run, **event_page, "observations_available": bool(event_page["observations"]),
            "events": [], "receipts": run.pop("receipts"), "outbox": run.pop("outbox"),
            "approvals": run.pop("approvals"), "source": "observation_index", "truncated": False}


def metrics(store: ObservationStore, filters: RunFilter) -> dict[str, Any]:
    clause, params = filters.clause()
    with store.connection() as connection:
        totals = dict(connection.execute(
            "SELECT COUNT(*) AS total,SUM(state='completed') AS completed,SUM(state='failed') AS failed,"
            "SUM(state='aborted') AS aborted,SUM(state IN ('completed','failed','aborted')) AS terminal,"
            "AVG(CASE WHEN state IN ('completed','failed','aborted') THEN (finished_at-started_at)*1000 END) AS mean_ms "
            "FROM runs r WHERE " + clause, params).fetchone())
        sample_clause = clause + " AND state IN ('completed','failed','aborted') AND finished_at>=started_at"
        count = connection.execute("SELECT COUNT(*) FROM runs r WHERE " + sample_clause, params).fetchone()[0]
        def percentile(fraction: float) -> float | None:
            if count < 20:
                return None
            return connection.execute("SELECT (finished_at-started_at)*1000 AS ms FROM runs r WHERE " + sample_clause +
                                      " ORDER BY ms LIMIT 1 OFFSET ?", [*params, math.ceil(count * fraction) - 1]).fetchone()[0]
        totals.update(sample_count=count, p50_ms=percentile(.5), p95_ms=percentile(.95))
        usage = dict(connection.execute(
            "SELECT COUNT(*) AS model_calls,SUM(e.total_tokens) AS total_tokens,SUM(e.input_tokens) AS input_tokens,"
            "SUM(e.output_tokens) AS output_tokens,SUM(e.cached_tokens) AS cached_tokens,"
            "SUM(e.total_tokens IS NOT NULL) AS usage_samples FROM events e JOIN runs r ON e.run_id=r.run_id "
            "WHERE e.kind='LlmCallFinished' AND " + clause, params).fetchone())
        totals.update(usage)
        totals["tool_calls"] = connection.execute("SELECT COUNT(*) FROM events e JOIN runs r ON e.run_id=r.run_id "
            "WHERE e.kind='ToolStarted' AND " + clause, params).fetchone()[0]
        components = [dict(row) for row in connection.execute(
            "SELECT e.layer,e.entity_id,COUNT(*) AS calls,SUM(e.status='failed') AS failures,AVG(e.elapsed_ms) AS mean_ms,"
            "SUM(e.elapsed_ms IS NOT NULL) AS timing_samples FROM events e JOIN runs r ON e.run_id=r.run_id "
            "WHERE e.phase='finish' AND " + clause + " GROUP BY e.layer,e.entity_id ORDER BY failures DESC,mean_ms DESC LIMIT 101", params)]
        trends = [dict(row) for row in connection.execute(
            "WITH filtered AS (SELECT date(r.created_at,'unixepoch') AS day,r.config_revision AS config_id,r.backend,r.model,r.state,"
            "CASE WHEN r.state IN ('completed','failed','aborted') AND r.finished_at>=r.started_at THEN "
            "(r.finished_at-r.started_at)*1000 END AS ms FROM runs r WHERE " + clause + "),"
            "ranked AS (SELECT *,ROW_NUMBER() OVER(PARTITION BY day,config_id,backend,model ORDER BY ms) AS position,"
            "COUNT(*) OVER(PARTITION BY day,config_id,backend,model) AS samples FROM filtered WHERE ms IS NOT NULL),"
            "percentiles AS (SELECT day,config_id,backend,model,MAX(CASE WHEN samples>=20 AND position=(samples*50+99)/100 "
            "THEN ms END) AS p50_ms,MAX(CASE WHEN samples>=20 AND position=(samples*95+99)/100 THEN ms END) AS p95_ms "
            "FROM ranked GROUP BY day,config_id,backend,model) "
            "SELECT f.day,f.config_id,f.backend,f.model,COUNT(*) AS total,SUM(f.state='failed') AS failed,"
            "AVG(f.ms) AS mean_ms,COUNT(f.ms) AS sample_count,p.p50_ms,p.p95_ms FROM filtered f LEFT JOIN percentiles p "
            "ON f.day IS p.day AND f.config_id IS p.config_id AND f.backend IS p.backend AND f.model IS p.model "
            "GROUP BY f.day,f.config_id,f.backend,f.model ORDER BY f.day DESC LIMIT 367", params)]
    return {"totals": totals, "components": components[:100], "trends": trends[:366], "generated_at": time.time(),
            "components_truncated": len(components) > 100, "trends_truncated": len(trends) > 366,
            "since": filters.since, "until": filters.until, "source": "observation_index"}
