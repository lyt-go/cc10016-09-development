"""会话管理业务规则：会话超时轮转、Cookie摘要刷新、请求计数追踪、代理归属回写。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "session_mgr"
LIST_FIELDS = ["会话编号", "关联源", "登录状态", "最后活动", "请求计数", "Cookie摘要", "UserAgent", "会话时长"]
REQUIRED_FIELDS = ["会话编号", "关联源"]
STATUS_ORDER = ["草稿", "活跃", "已过期", "已失效"]
ACTION_RULES = {"标记活跃": "活跃", "标记过期": "已过期", "标记失效": "已失效"}
NEGATIVE_ACTIONS = ["标记失效"]
# 会话管理是代理调度结果的读取方，不是写入方：这里只引用代理池给出的归属依据。
DISPATCH_READER = "session_mgr"


def _check_session_expiry(entry: dict[str, Any]) -> bool:
    """检查会话是否超过时长限制（模拟）。"""
    try:
        duration = int(str(entry.get("会话时长") or "3600").replace("s", "").replace("秒", ""))
    except (ValueError, TypeError):
        duration = 3600
    req_count = int(str(entry.get("请求计数") or "0"))
    # 模拟：请求数超过阈值或固定超时即过期
    return req_count > duration / 10


class SessionMgrService:
    def _dispatch_view(self, session_id: int) -> dict[str, Any] | None:
        """只读拉取代理池调度结果，保持与代理池一侧同源。"""
        from app.services.proxy_pool import ProxyPoolService

        return ProxyPoolService().get_dispatch_result(session_id, reader=DISPATCH_READER)

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("会话编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        enriched = []
        logs = store.pipeline_state("session_request_logs") or {}
        for row in rows[start:start + size]:
            d = dict(row)
            expired = _check_session_expiry(d)
            d["_expired"] = expired
            d["_request_log"] = logs.get(str(d["id"]), [])
            d["_dispatch"] = self._dispatch_view(int(d["id"]))
            enriched.append(d)
        return enriched, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        d = dict(entry)
        d["_expired"] = _check_session_expiry(d)
        logs = store.pipeline_state("session_request_logs") or {}
        d["_request_log"] = logs.get(str(entry_id), [])
        d["_dispatch"] = self._dispatch_view(entry_id)
        return d

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["请求计数"] = "0"
        rows.append(entry)
        # 代理绑定不在登记时直接落库：会话只能引用代理池调度结果（同源），
        # 调用方可以显式申请一次调度，而不是自行拼一个 session_to_proxy 关系。
        if values.get("绑定代理ID"):
            dispatch = self.request_dispatch(entry["id"], cross_region=bool(values.get("跨域调度")))
            entry["_dispatch"] = dispatch
        return entry, []

    def request_dispatch(self, entry_id: int, *, cross_region: bool = False,
                         authorization: dict[str, Any] | None = None,
                         expected_version: int | None = None) -> dict[str, Any]:
        """会话侧发起调度申请，真正写入仍由代理池调度一侧完成。"""
        from app.services.proxy_pool import DISPATCH_OWNER, ProxyPoolService

        if store.find(MODULE, entry_id) is None:
            return {"ok": False, "code": "session_missing", "message": f"采集会话 {entry_id} 不存在或已归档"}
        return ProxyPoolService().dispatch_for_session(
            entry_id,
            cross_region=cross_region,
            authorization=authorization,
            expected_version=expected_version,
            writer=DISPATCH_OWNER,
        )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集会话 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于会话管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"采集会话已{action}"

    def record_request(self, entry_id: int, url: str, latency_ms: float,
                       *, writer: str = DISPATCH_READER) -> dict[str, Any]:
        """记录会话请求并写入代理编号与归属依据。

        代理编号只能来自代理池调度结果：未分配、或调度结果中的跨域代理没有授权时，
        请求日志不落全量记录，返回拒绝原因。请求日志是会话管理的自有数据，
        其他模块的写入同样按越权处理。
        """
        if writer != DISPATCH_READER:
            return {
                "ok": False,
                "code": "forbidden_writer",
                "message": f"会话请求记录只能由 {DISPATCH_READER} 写入，{writer} 的写入被拒绝",
            }
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False, "error": "会话不存在"}
        dispatch = self._dispatch_view(entry_id) or {}
        proxy_id = dispatch.get("proxy_id")
        if not proxy_id:
            return {
                "ok": False,
                "code": "no_dispatch",
                "message": "该会话尚未取得有效代理调度结果，请求未写入日志",
                "session_id": entry_id,
            }
        authorization = dispatch.get("authorization") or {}
        if dispatch.get("scope") == "跨域" and not (authorization.get("授权编号") and authorization.get("授权人")):
            return {
                "ok": False,
                "code": "unauthorized_cross_region",
                "message": "跨域代理缺少授权依据，请求未写入日志",
                "session_id": entry_id,
                "proxy_id": proxy_id,
            }
        try:
            count = int(str(entry.get("请求计数") or "0")) + 1
        except (ValueError, TypeError):
            count = 1
        entry["请求计数"] = str(count)
        entry["最后活动"] = "2026-11-16T00:00:00Z"
        logs = store.pipeline_state("session_request_logs") or {}
        log = logs.setdefault(str(entry_id), [])
        log.append({
            "url": url,
            "latency": latency_ms,
            "count": count,
            "proxy_id": int(proxy_id),
            "proxy_code": dispatch.get("proxy_code"),
            "proxy_region": dispatch.get("proxy_region"),
            "session_region": dispatch.get("region_raw") or dispatch.get("region"),
            "affiliation_basis": (dispatch.get("affiliation") or {}).get("basis"),
            "scope": dispatch.get("scope"),
            "authorization": authorization,
            "dispatch_version": dispatch.get("version"),
            "audit_reason": (dispatch.get("audit") or {}).get("reason"),
        })
        if len(log) > 100:
            logs[str(entry_id)] = log[-100:]
        store.set_pipeline_state("session_request_logs", logs)
        return {
            "ok": True,
            "request_count": count,
            "proxy_id": int(proxy_id),
            "proxy_code": dispatch.get("proxy_code"),
            "affiliation_basis": (dispatch.get("affiliation") or {}).get("basis"),
            "scope": dispatch.get("scope"),
        }

    def rotate_session(self, source_id: int) -> dict[str, Any] | None:
        """为指定采集源轮替到一个活跃会话。"""
        rels = store.get_relations("schedule_to_session", target_module=MODULE)
        for rel in rels:
            sess = store.find(MODULE, rel["target_id"])
            if sess and sess.get("关联源") == str(source_id) and sess.get("status") == "活跃":
                return dict(sess)
        # 查找该源下任意活跃会话
        for row in store.rows(MODULE):
            if row.get("关联源") == str(source_id) and row.get("status") == "活跃":
                return dict(row)
        return None
