# 将研究运行及阶段原始输出归档到 Neo4j，并支持历史查询和追踪恢复。
"""Archive research runs and retained stage reports in the existing Neo4j database."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, Field

from open_deep_research.configuration import (
    DEFAULT_PUBLIC_OPINION_AGENTS,
    Configuration,
)
from open_deep_research.observability.langsmith import WORKFLOW_NAME, langsmith_project
from open_deep_research.research_graph.schema import stable_id


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
    source: str = "web"
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
    source: str = "web",
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
    usage_keys = ("model_calls", "input_tokens", "output_tokens", "total_tokens")
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


# 管理与智能体证据节点分开的报告归档数据。
class Neo4jHistoryStore:
    """Keep report archives separate from agent evidence retrieval nodes."""

    # 复用 Neo4j 驱动并初始化历史归档专用约束和索引。
    def __init__(self, driver: Any, database: str | None = None) -> None:
        """Reuse a Neo4j connection and initialize archive-only schema."""
        self.driver = driver
        self.database = database
        try:
            self._query(
                "CREATE CONSTRAINT research_history_id IF NOT EXISTS "
                "FOR (h:ResearchHistory) REQUIRE h.id IS UNIQUE"
            )
            self._query(
                "CREATE INDEX research_history_completed IF NOT EXISTS "
                "FOR (h:ResearchHistory) ON (h.completed_at)"
            )
        except Exception:
            self.close()
            raise

    # 在指定数据库会话中执行参数化查询，区分读写操作。
    def _query(self, cypher: str, *, read: bool = False, **params: Any) -> list[Any]:
        try:
            result = self.driver.execute_query(
                cypher,
                parameters_=params,
                database_=self.database,
                routing_="r" if read else "w",
            )
            return list(result.records)
        except Exception as exc:
            raise HistoryUnavailable(
                "研究历史暂时无法访问，请检查 Neo4j 服务和连接配置。"
            ) from exc

    # 幂等写入归档和阶段输出，避免重复导入覆盖已有报告。
    def save(self, records: list[HistoryRecord]) -> int:
        """Insert archives idempotently without replacing existing reports."""
        if not records:
            return 0
        # One logical run can have multiple resumed LangSmith traces. Preserve
        # the latest completed version within an import, and never overwrite a
        # report that the web server has already archived.
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
        rows = []
        for record in unique.values():
            row = record.model_dump(
                exclude={"request", "usage", "role_reports"}, exclude_none=True
            )
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
            rows.append(row)
        result = self._query(
            """
            UNWIND $rows AS row
            OPTIONAL MATCH (existing:ResearchHistory {id: row.id})
            WITH row, existing IS NULL AS is_new
            MERGE (h:ResearchHistory {id: row.id})
            ON CREATE SET h += row
            WITH h, row, is_new,
                row.source = 'python_worker' AND row.owner = h.owner
                  AND row.run_state_version > coalesce(h.run_state_version, 0) AS newer_run
            FOREACH (_ IN CASE WHEN newer_run THEN [1] ELSE [] END | SET h += row)
            WITH h, row, is_new,
                coalesce(h.report, '') = '' AND row.report <> '' AS add_report,
                coalesce(h.stage_count, 0) < row.stage_count AS add_stages
            SET h.langsmith_run_id = coalesce(h.langsmith_run_id, row.langsmith_run_id)
            SET h.failed_stage = CASE WHEN coalesce(h.failed_stage, '') = '' THEN row.failed_stage ELSE h.failed_stage END,
                h.usage_json = CASE WHEN coalesce(h.usage_json, '{}') = '{}' THEN row.usage_json ELSE h.usage_json END
            FOREACH (_ IN CASE WHEN add_report THEN [1] ELSE [] END |
                SET h.report = row.report, h.status = row.status,
                    h.completed_at = row.completed_at, h.usage_json = row.usage_json,
                    h.preview = row.preview, h.search_text = row.search_text)
            FOREACH (_ IN CASE WHEN add_stages THEN [1] ELSE [] END |
                SET h.role_reports_json = row.role_reports_json, h.stage_count = row.stage_count,
                    h.preview = CASE WHEN h.report = '' THEN row.preview ELSE h.preview END,
                    h.search_text = coalesce(h.search_text, h.topic + '\\n' + h.report) + '\\n' + row.role_reports_json)
            RETURN sum(CASE WHEN is_new THEN 1 ELSE 0 END) AS imported
            """,
            rows=rows,
        )
        return int(result[0]["imported"] or 0)

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
        # Aggregate the total separately inside the same database statement so
        # an empty page still returns its count and pagination stays stable.
        result = self._query(
            """
            MATCH (h:ResearchHistory)
            WHERE (coalesce(h.owner, '') = '' OR h.owner = $owner)
              AND ($query = '' OR toLower(coalesce(h.search_text, h.topic + '\\n' + h.report)) CONTAINS $query)
            WITH count(h) AS total
            CALL () {
                MATCH (h:ResearchHistory)
                WHERE (coalesce(h.owner, '') = '' OR h.owner = $owner)
                  AND ($query = '' OR toLower(coalesce(h.search_text, h.topic + '\\n' + h.report)) CONTAINS $query)
                WITH h ORDER BY h.completed_at DESC, h.id DESC
                SKIP $offset LIMIT $limit
                RETURN collect(h { .id, .run_id, .topic, .started_at,
                    .completed_at, .source, .request_json, .usage_json,
                    .status, .stage_count, .error_message,
                    preview: coalesce(h.preview, left(h.report, 160)) }) AS items
            }
            RETURN total, items
            """,
            read=True,
            query=query.strip().lower(),
            limit=limit,
            offset=offset,
            owner=owner,
        )
        return {
            "items": [self._decode(item) for item in result[0]["items"]],
            "total": result[0]["total"],
            "limit": limit,
            "offset": offset,
            "storage": "neo4j",
        }

    # 读取单条归档，并仅统计该运行自身的证据规模。
    def get(
        self, history_id: str, *, owner: str | None = None
    ) -> dict[str, Any] | None:
        """Reopen an archive and count only evidence from its own run."""
        rows = self._query(
            """
            MATCH (h:ResearchHistory {id: $id})
            WHERE coalesce(h.owner, '') = '' OR h.owner = $owner
            OPTIONAL MATCH (n:ResearchNode {run_id: h.run_id})
            RETURN properties(h) AS record,
                count(CASE WHEN n:Source THEN 1 END) AS sources,
                count(CASE WHEN n:Evidence THEN 1 END) AS evidence,
                count(CASE WHEN n:Finding THEN 1 END) AS findings
            """,
            read=True,
            id=history_id,
            owner=owner,
        )
        if not rows:
            return None
        row = rows[0]
        record = self._decode(dict(row["record"]))
        record["graph"] = {key: row[key] for key in ("sources", "evidence", "findings")}
        return record

    # 将数据库保存的结构化字段还原为前端可用的数据。
    @staticmethod
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

    # 关闭历史数据库驱动及其连接池。
    def close(self) -> None:
        """Close the history connection pool."""
        self.driver.close()


_HISTORY_STORE_LOCK = RLock()


# 按研究图凭据延迟创建可复用的历史存储，不强制启用追踪。
@lru_cache(maxsize=1)
def _cached_history_store() -> Neo4jHistoryStore:
    """Reuse Research Graph credentials without making tracing mandatory."""
    from neo4j import GraphDatabase

    config = Configuration.from_runnable_config({})
    if not config.research_graph_uri:
        raise HistoryUnavailable(
            "请先配置 Neo4j 的 RESEARCH_GRAPH_URI 以保存研究历史。"
        )
    auth = (
        (config.research_graph_username, config.research_graph_password)
        if config.research_graph_username and config.research_graph_password
        else None
    )
    driver = GraphDatabase.driver(
        config.research_graph_uri,
        auth=auth,
        connection_timeout=5,
        connection_acquisition_timeout=10,
        max_transaction_retry_time=3,
    )
    return Neo4jHistoryStore(driver, database=config.research_graph_database)


# 为并发历史请求复用同一缓存连接池。
def get_history_store() -> Neo4jHistoryStore:
    """Share one connection pool across simultaneous history requests."""
    with _HISTORY_STORE_LOCK:
        return _cached_history_store()


# 在服务关闭时释放已初始化的历史存储。
def close_history_store() -> None:
    """Release the lazily initialized store on web-server shutdown."""
    with _HISTORY_STORE_LOCK:
        if _cached_history_store.cache_info().currsize:
            _cached_history_store().close()
        _cached_history_store.cache_clear()


# 兼容消息对象及内容块，提取可归档文本。
def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return str(getattr(message, "content", "") or "")
    content = message.get("content", "")
    # Older LangChain traces can store constructor-serialized messages.
    if not content and isinstance(message.get("kwargs"), dict):
        content = message["kwargs"].get("content", "")
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content or "")


# 从状态增量提取正式角色报告，保留阶段信息并排除私有记忆。
def stage_reports_from_state(
    state: dict[str, Any], *, stage_id: str = "", completed_at: str = ""
) -> list[HistoryStage]:
    """Extract formal agent reports from state updates without copying memory."""
    stages = []
    for role, data in (state.get("agents") or {}).items():
        if role not in DEFAULT_PUBLIC_OPINION_AGENTS or not isinstance(data, dict):
            continue
        report = data.get("report")
        if isinstance(report, dict):
            report = report.get("value") or report.get("content")
        if isinstance(report, str) and report.strip():
            stages.append(
                HistoryStage(
                    role=role,
                    content=report,
                    stage_id=stage_id,
                    completed_at=completed_at,
                )
            )
    return stages


# 从已结束的研究追踪构造归档，兼容没有最终报告的失败运行。
def record_from_langsmith(run: Any) -> HistoryRecord | None:
    """Extract ended research runs, including failures without a final report."""
    if not run.end_time or run.parent_run_id:
        return None
    outputs = run.outputs or {}
    report_state = outputs.get("report") or {}
    report = report_state.get("final") if isinstance(report_state, dict) else None
    report = report or outputs.get("final_report")
    report = report if isinstance(report, str) and report.strip() else ""
    metadata = (run.extra or {}).get("metadata") or {}
    inputs = run.inputs or {}
    # A formal report identifies an older/renamed research graph. Without one,
    # require a known workflow or its metadata plus human-message inputs so
    # unrelated root LLM/structured-output spans never become history entries.
    known_workflow = run.name in {WORKFLOW_NAME, "LangGraph"}
    marked_workflow = (
        metadata.get("business_scenario") == "public_opinion_risk"
        or metadata.get("workflow") == WORKFLOW_NAME
    )
    if not report and not (
        known_workflow or (marked_workflow and inputs.get("messages"))
    ):
        return None
    messages = inputs.get("messages") or []
    human_messages = [
        message
        for message in messages
        if isinstance(message, dict)
        and (
            message.get("type") in {"human", "user"}
            or message.get("role") == "user"
            or (message.get("id") or [""])[-1] == "HumanMessage"
        )
    ]
    topic = next(
        (
            _message_text(message)
            for message in human_messages or messages
            if _message_text(message)
        ),
        "",
    )
    topic = topic or str((outputs.get("workflow") or {}).get("brief") or "未命名研究")
    research = outputs.get("research") or {}
    run_id = str(
        research.get("run_id")
        or metadata.get("research_run_id")
        or metadata.get("thread_id")
        or run.id
    )
    request = {
        "topic": topic,
        "model": metadata.get("research_model"),
        "search_api": metadata.get("search_api"),
        "mode": metadata.get("web_research_mode"),
        "org_context": metadata.get("organization_context", ""),
        "rag_enabled": metadata.get("rag_enabled", False),
    }
    status = "completed" if report and not run.error else "incomplete"
    if run.error:
        status = "cancelled" if "CancelledError" in str(run.error) else "failed"
    return history_record(
        run_id=run_id,
        request={key: value for key, value in request.items() if value is not None},
        report=report,
        usage=(outputs.get("runtime") or {}).get("budget") or {},
        started_at=utc_timestamp(run.start_time),
        completed_at=utc_timestamp(run.end_time),
        source="langsmith",
        langsmith_run_id=str(run.id),
        status=status,
        role_reports=stage_reports_from_state(
            outputs, completed_at=utc_timestamp(run.end_time)
        ),
        error_message="研究运行失败，已保留原始主题、参数及已完成的阶段内容。"
        if run.error
        else "",
    )


# 从失败运行的子追踪中恢复已成功产生的原始角色输出。
def recover_langsmith_stages(record: HistoryRecord, *, client: Any) -> None:
    """Recover original successful agent outputs beneath one failed trace."""
    agent_names = [f"{role}_agent" for role in DEFAULT_PUBLIC_OPINION_AGENTS]
    nodes = list(
        client.list_runs(
            trace_id=record.langsmith_run_id,
            run_type="chain",
            filter="or("
            + ",".join(f'eq(name, "{name}")' for name in agent_names)
            + ")",
            select=[
                "id",
                "name",
                "start_time",
                "end_time",
                "inputs",
                "outputs",
                "error",
            ],
        )
    )
    nodes.sort(key=lambda node: utc_timestamp(node.start_time))
    stages = []
    for node in nodes:
        # Inputs carry cumulative usage even when the following node fails.
        budget = ((node.inputs or {}).get("runtime") or {}).get("budget") or {}
        if budget:
            record.usage = {
                key: budget[key]
                for key in (
                    "model_calls",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
                if key in budget
            }
        if node.error:
            record.failed_stage = node.name.removesuffix("_agent")
        elif node.end_time:
            stages.extend(
                stage_reports_from_state(
                    node.outputs or {},
                    stage_id=str(node.id),
                    completed_at=utc_timestamp(node.end_time),
                )
            )
    if stages:
        record.role_reports = stages


# 导入已结束的研究追踪及保留阶段输出，并汇总导入结果。
def import_langsmith_history(
    store: Neo4jHistoryStore, *, client: Any = None
) -> dict[str, Any]:
    """Archive ended research traces, including retained outputs of failed runs."""
    if client is None:
        if not (
            os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGSMITH_ENDPOINT")
        ):
            raise HistoryUnavailable("请先配置 LangSmith 访问凭据，再导入旧研究。")
        from langsmith import Client

        client = Client(timeout_ms=10_000)
    try:
        runs = client.list_runs(
            project_name=langsmith_project(),
            is_root=True,
            run_type="chain",
            select=[
                "id",
                "name",
                "parent_run_id",
                "start_time",
                "end_time",
                "inputs",
                "outputs",
                "extra",
                "error",
            ],
        )
        scanned = skipped = imported = 0
        batch = []
        for run in runs:
            scanned += 1
            record = record_from_langsmith(run)
            if record is None:
                skipped += 1
                continue
            if not record.report:
                recover_langsmith_stages(record, client=client)
            batch.append(record)
            if len(batch) == 50:
                imported += store.save(batch)
                batch.clear()
        imported += store.save(batch)
    except HistoryUnavailable:
        raise
    except Exception as exc:
        raise HistoryUnavailable(
            "LangSmith 历史读取失败，请检查服务和访问凭据后重试。"
        ) from exc
    return {"imported": imported, "scanned": scanned, "skipped": skipped}
