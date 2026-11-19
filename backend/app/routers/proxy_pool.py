"""代理池调度接口：维护代理节点，覆盖提交校验、标记可用、标记失效等动作。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services.proxy_pool import ProxyPoolService

router = APIRouter(prefix="/api/proxy_pool", tags=["代理池调度"])

service = ProxyPoolService()

LIST_FIELDS = ["节点编号", "代理地址", "端口号", "协议类型", "地域标识", "响应延迟", "成功率", "失效次数"]
STATUSES = ["草稿", "待校验", "可用", "已失效"]


def query_filters(request: Request) -> dict[str, str]:
    """只接受当前模块声明过的字段，忽略分页等保留参数。"""
    return {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in LIST_FIELDS and value.strip()
    }


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按节点编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、可用、已失效"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    """按页面字段、节点编号关键字与状态过滤代理池调度列表。"""
    items, total = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    """下载当前筛选条件下的 UTF-8 CSV；必须声明在 /{entry_id} 之前。"""
    items, _ = service.list_entries(
        filters=query_filters(request), keyword=keyword, status=status, page=1, size=1000000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *LIST_FIELDS, "status"])
    for item in items:
        row = []
        for value in [item.get("id"), *(item.get(field, "") for field in LIST_FIELDS), item.get("status", "")]:
            text = "" if value is None else str(value)
            row.append("'" + text if text.startswith(("=", "+", "-", "@")) else text)
        writer.writerow(row)
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="proxy_pool.csv"'})


@router.get("/dispatch", response_model=dict)
def list_dispatch_results() -> dict:
    """受控共享：读取全部会话的代理调度结果（只读快照）。"""
    return {"ok": True, "items": service.list_dispatch_results()}


@router.get("/dispatch/conflicts", response_model=dict)
def list_dispatch_conflicts() -> dict:
    """版本冲突仲裁记录：谁在哪个版本上被拒、按什么规则仲裁。"""
    from app.store import store

    return {"ok": True, "items": store.pipeline_state("proxy_dispatch_conflicts") or []}


@router.post("/dispatch", response_model=dict)
def dispatch_session(payload: dict[str, object]) -> dict:
    """为会话编排代理并生成调度结果；跨域必须带授权，版本冲突按先后仲裁。"""
    session_id = payload.get("session_id")
    if session_id is None:
        return {"ok": False, "code": "bad_session", "message": "缺少 session_id"}
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return {"ok": False, "code": "bad_session", "message": "session_id 必须是整数"}
    return service.dispatch_for_session(
        sid,
        cross_region=bool(payload.get("cross_region")),
        authorization=payload.get("authorization") if isinstance(payload.get("authorization"), dict) else None,
        expected_version=(
            int(payload["expected_version"]) if str(payload.get("expected_version") or "").strip().isdigit()
            else None
        ),
        writer=str(payload.get("writer") or "proxy_pool"),
    )


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条代理节点明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"代理节点 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: dict[str, object]) -> ActionResult:
    """登记一条代理节点，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="代理节点已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    """对单条代理节点执行提交校验、标记可用、标记失效；不允许的动作会被拦下并说明原因。"""
    entry, message = service.run_action(entry_id, payload.action.strip())
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/select")
def select_proxy(payload: dict[str, object]) -> dict:
    """按地域等条件选择最优代理。"""
    region = payload.get("region")
    proxy = service.select_proxy(region=str(region) if region else None)
    if proxy is None:
        return {"ok": False, "error": "无可用代理"}
    return {"ok": True, "proxy": proxy}


@router.post("/{entry_id}/record")
def record_request(entry_id: int, payload: dict[str, object]) -> dict:
    """记录代理请求结果。"""
    success = bool(payload.get("success", True))
    latency = float(payload.get("latency", 0))
    return service.record_request(entry_id, success, latency)
