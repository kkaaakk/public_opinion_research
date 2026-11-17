# 将研究运行及阶段原始输出归档到 PostgreSQL，并支持历史查询。
"""Archive research runs and retained stage reports in PostgreSQL."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from open_deep_research.ids import stable_id


# 表示可安全展示给用户的历史服务不可用错误。
class HistoryUnavailable(RuntimeError):
    """Indicate a history service failure using a safe, user-facing message."""


# 保存一次角色阶段的原始输出，区分连续补充轮次。
class HistoryStage(BaseModel):
    """Preserve one original role output, including successive research rounds."""

    role: str
    content: str
    stage_id: str = ""
    completed_at: str = ""


# 描述研究归档，兼容完成、失败、取消及未完成的运行。
class HistoryRecord(BaseModel):
    """Represent a research run even when final report generation failed."""

    id: str
    run_id: str
    topic: str
    report: str = ""
    started_at: str
    completed_at: str
    request: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    source: str = "dbos"
    langsmith_run_id: str | None = None
    status: Literal["completed", "failed", "cancelled", "incomplete"] = "completed"
    role_reports: list[HistoryStage] = Field(default_factory=list)
    error_message: str = ""
    failed_stage: str = ""
    owner: str = ""
    research_session_id: str = ""
    run_state_version: int = 0


# 将时间规范为 UTC 字符串，便于排序及浏览器时区转换。
def utc_timestamp(value: datetime | None = None) -> str:
    """Normalize timestamps for sorting and browser timezone conversion."""
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


# 从请求、报告和消耗构造归档记录，排除凭据及无关状态。
def history_record(
    *,
    run_id: str,
    request: dict[str, Any],
    report: str,
    usage: dict[str, Any],
    started_at: str,
    completed_at: str | None = None,
    source: str = "dbos",
    langsmith_run_id: str | None = None,
    status: Literal["completed", "failed", "cancelled", "incomplete"] = "completed",
    role_reports: list[HistoryStage] | None = None,
    error_message: str = "",
    failed_stage: str = "",
) -> HistoryRecord:
    """Build an archive without credentials or unrelated workflow state."""
    request_keys = (
        "topic",
        "model",
        "search_api",
        "mode",
        "org_context",
        "rag_enabled",
    )
    usage_keys = (
        "model_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "usage_complete",
    )
    return HistoryRecord(
        id=stable_id("HISTORY", run_id, length=32),
        run_id=run_id,
        topic=next(
            (
                line.strip()
                for line in str(request.get("topic") or "").splitlines()
                if line.strip()
            ),
            "未命名研究",
        ),
        report=report,
        started_at=started_at,
        completed_at=completed_at or utc_timestamp(),
        request={key: request[key] for key in request_keys if key in request},
        usage={key: usage[key] for key in usage_keys if key in usage},
        source=source,
        langsmith_run_id=langsmith_run_id,
        status=status,
        role_reports=role_reports or [],
        error_message=error_message,
        failed_stage=failed_stage,
    )


# 用 PostgreSQL 保存报告归档，与 DBOS 请求表共享同一业务库。
class HistoryStore:
    """Persist report archives idempotently in the business PostgreSQL schema."""

    def __init__(self, engine) -> None:
        self.engine = engine

    # 幂等写入归档和阶段输出，避免重复导入覆盖已有报告。
    def save(self, records: list[HistoryRecord]) -> int:
        """Upsert archives idempotently without replacing existing reports."""
        if not records:
            return 0
        unique = _preferred_records(records)
        imported = 0
        try:
            with self.engine.begin() as db:
                for record in unique.values():
                    existing = _read_row(db, record.id, lock=True)
                    merged = _merge_record(existing, record)
                    _write_row(db, merged)
                    if existing is None:
                        imported += 1
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("研究历史暂时无法写入，请检查业务数据库连接。") from exc
        return imported

    # 按搜索词和分页返回运行摘要，不加载完整报告正文。
    def list(
        self,
        *,
        query: str = "",
        limit: int = 10,
        offset: int = 0,
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Return completed and failed run summaries without full report bodies."""
        needle = f"%{query.strip().lower()}%"
        where = "(:owner = '' OR coalesce(owner, '') = '' OR owner = :owner) AND (:needle = '%%' OR search_text ILIKE :needle)"
        params = {
            "owner": owner or "",
            "needle": needle,
            "limit": limit,
            "offset": offset,
        }
        try:
            with self.engine.connect() as db:
                total = db.scalar(
                    text(f"SELECT count(*) FROM por_domain.history WHERE {where}"),
                    params,
                )
                rows = db.execute(
                    text(
                        f"""
                        SELECT id, run_id, topic, started_at, completed_at, source,
                               request_json, usage_json, status, stage_count,
                               error_message,
                               coalesce(nullif(preview, ''), left(report, 160)) AS preview
                        FROM por_domain.history
                        WHERE {where}
                        ORDER BY completed_at DESC, id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                ).mappings()
                items = [_decode(dict(row)) for row in rows]
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("研究历史暂时无法访问，请检查业务数据库连接。") from exc
        return {
            "items": items,
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "storage": "postgres",
        }

    # 读取单条归档及当时的完整报告。
    def get(
        self, history_id: str, *, owner: str | None = None
    ) -> dict[str, Any] | None:
        """Reopen one archive within the requesting owner scope."""
        try:
            with self.engine.connect() as db:
                row = db.execute(
                    text(
                        """
                        SELECT * FROM por_domain.history
                        WHERE id = :id
                          AND (:owner = '' OR coalesce(owner, '') = '' OR owner = :owner)
                        """
                    ),
                    {"id": history_id, "owner": owner or ""},
                ).mappings().first()
        except SQLAlchemyError as exc:
            raise HistoryUnavailable("研究历史暂时无法访问，请检查业务数据库连接。") from exc
        return _decode(dict(row)) if row else None


# 只创建不带连接池的存储门面，连接由业务引擎统一管理。
def get_history_store() -> HistoryStore:
    """Return a stateless archive facade over the business PostgreSQL engine."""
    from open_deep_research.workflow.dbos_config import domain_engine

    return HistoryStore(domain_engine())


# 合并同一批导入中的多个版本，保留完整报告和全部阶段。
def _preferred_records(records: list[HistoryRecord]) -> dict[str, HistoryRecord]:
    unique: dict[str, HistoryRecord] = {}
    for record in records:
        previous = unique.get(record.id)
        if previous is None:
            unique[record.id] = record
            continue
        # A later failed resume must not hide an earlier complete report.
        preferred = max(
            (previous, record),
            key=lambda item: (bool(item.report), item.completed_at),
        )
        stages = {}
        for stage in [*previous.role_reports, *record.role_reports]:
            stages[(stage.role, stage.stage_id or stage.content)] = stage
        unique[record.id] = preferred.model_copy(
            update={
                "role_reports": sorted(
                    stages.values(),
                    key=lambda stage: stage.completed_at,
                )
            }
        )
    return unique


# 读取现有归档行并锁定，避免并发归档相互覆盖。
def _read_row(db, history_id: str, *, lock: bool) -> HistoryRecord | None:
    row = db.execute(
        text("SELECT * FROM por_domain.history WHERE id=:id" + (" FOR UPDATE" if lock else "")),
        {"id": history_id},
    ).mappings().first()
    if row is None:
        return None
    return HistoryRecord(**_decode(dict(row)))


# 按既有优先级合并新归档：新执行版本整体替换，否则只补全缺失内容。
def _merge_record(
    existing: HistoryRecord | None, incoming: HistoryRecord
) -> HistoryRecord:
    if existing is None:
        return incoming
    newer_run = (
        incoming.source in {"python_worker", "dbos"}
        and incoming.owner == existing.owner
        and incoming.run_state_version > existing.run_state_version
    )
    if newer_run:
        return incoming.model_copy(
            update={
                "langsmith_run_id": existing.langsmith_run_id
                or incoming.langsmith_run_id
            }
        )
    stages = {
        (stage.role, stage.stage_id or stage.content): stage
        for stage in existing.role_reports
    }
    stages.update(
        {
            (stage.role, stage.stage_id or stage.content): stage
            for stage in incoming.role_reports
        }
    )
    return existing.model_copy(
        update={
            "report": existing.report or incoming.report,
            "status": incoming.status if (not existing.report and incoming.report) else existing.status,
            "completed_at": incoming.completed_at if (not existing.report and incoming.report) else existing.completed_at,
            "usage": existing.usage or incoming.usage,
            "langsmith_run_id": existing.langsmith_run_id or incoming.langsmith_run_id,
            "failed_stage": existing.failed_stage or incoming.failed_stage,
            "role_reports": sorted(stages.values(), key=lambda stage: stage.completed_at),
        }
    )


# 将一条归档写入 PostgreSQL，字段序列化保持前端协议不变。
def _write_row(db, record: HistoryRecord) -> None:
    row = record.model_dump(exclude={"request", "usage", "role_reports"})
    row["request_json"] = json.dumps(record.request, ensure_ascii=False)
    row["usage_json"] = json.dumps(record.usage, ensure_ascii=False)
    row["role_reports_json"] = json.dumps(
        [stage.model_dump() for stage in record.role_reports],
        ensure_ascii=False,
    )
    row["stage_count"] = len(record.role_reports)
    row["preview"] = (
        record.report
        or (
            record.role_reports[0].content
            if record.role_reports
            else record.error_message
        )
    )[:160]
    row["search_text"] = "\n".join(
        [
            record.topic,
            str(record.request.get("topic") or ""),
            record.report,
            *(stage.content for stage in record.role_reports),
        ]
    )
    columns = ", ".join(row)
    values = ", ".join(f":{key}" for key in row)
    updates = ", ".join(f"{key}=excluded.{key}" for key in row if key != "id")
    db.execute(
        text(
            f"INSERT INTO por_domain.history ({columns}) VALUES ({values}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}"
        ),
        row,
    )


# 将数据库保存的结构化字段还原为前端可用的数据。
def _decode(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    for key in ("request", "usage"):
        result[key] = json.loads(result.pop(f"{key}_json", "{}"))
    result["role_reports"] = json.loads(result.pop("role_reports_json", "[]"))
    result["status"] = result.get("status") or (
        "completed"
        if result.get("report") or result.get("preview")
        else "incomplete"
    )
    result["stage_count"] = result.get("stage_count") or len(result["role_reports"])
    result.pop("search_text", None)
    return result


__all__ = [
    "HistoryRecord",
    "HistoryStage",
    "HistoryStore",
    "HistoryUnavailable",
    "get_history_store",
    "history_record",
    "utc_timestamp",
]
