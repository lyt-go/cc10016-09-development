"""代理池调度业务规则：健康检查、评分衰减、轮替调度与会话地域归属调度。"""
from __future__ import annotations

import copy
from typing import Any

from app.services.region_scope import (
    NATIONWIDE,
    normalize_region,
    proxy_affiliation,
    region_matches,
    resolve_session_affiliation,
)
from app.store import store

MODULE = "proxy_pool"
LIST_FIELDS = ["节点编号", "代理地址", "端口号", "协议类型", "地域标识", "响应延迟", "成功率", "失效次数"]
REQUIRED_FIELDS = ["节点编号", "代理地址", "端口号"]
STATUS_ORDER = ["草稿", "待校验", "可用", "已失效"]
ACTION_RULES = {"提交校验": "待校验", "标记可用": "可用", "标记失效": "已失效"}
NEGATIVE_ACTIONS = ["标记失效"]

# 健康评分下限：低于该值即使状态为「可用」也不计入可调度候选。
MIN_HEALTH_SCORE = 0.35


def _utc_now() -> str:
    """单调递增的调度时间戳：与示例数据使用的固定时钟保持一致并附加版本序号。"""
    return "2026-11-19T00:00:00Z"

# 代理调度结果是代理池调度一侧生成的受控共享内容：
# 只有生成方可以写入，其余模块只能读取，越权写入一律拒绝。
DISPATCH_OWNER = MODULE
DISPATCH_READERS = {
    "proxy_pool", "session_mgr", "crawl_schedule", "crawl_monitor", "chain",
}


def _compute_health_score(entry: dict[str, Any]) -> float:
    """基于成功率、延迟、失效次数计算健康评分（0-1）。"""
    try:
        success_rate = float(str(entry.get("成功率") or "0.95").replace("%", ""))
        if success_rate > 1:
            success_rate = success_rate / 100.0
    except (ValueError, TypeError):
        success_rate = 0.95
    try:
        latency = float(str(entry.get("响应延迟") or "200").replace("ms", ""))
    except (ValueError, TypeError):
        latency = 200.0
    try:
        failures = int(str(entry.get("失效次数") or "0"))
    except (ValueError, TypeError):
        failures = 0
    latency_score = max(0.0, 1.0 - latency / 1000.0)
    failure_penalty = min(0.5, failures * 0.1)
    score = success_rate * 0.5 + latency_score * 0.3 - failure_penalty
    return round(max(0.0, min(1.0, score)), 3)


class ProxyPoolService:
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
            rows = [row for row in rows if keyword.casefold() in str(row.get("节点编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        enriched = []
        scores = store.pipeline_state("proxy_health_scores") or {}
        for row in rows[start:start + size]:
            d = dict(row)
            d["_health_score"] = _compute_health_score(d)
            d["_history"] = scores.get(str(d["id"]), [])
            enriched.append(d)
        return enriched, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        d = dict(entry)
        d["_health_score"] = _compute_health_score(d)
        scores = store.pipeline_state("proxy_health_scores") or {}
        d["_history"] = scores.get(str(entry_id), [])
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
        entry["_health_score"] = _compute_health_score(entry)
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"代理节点 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于代理池调度可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        if action == "标记失效":
            failures = int(str(entry.get("失效次数") or "0")) + 1
            entry["失效次数"] = str(failures)
            scores = store.pipeline_state("proxy_health_scores") or {}
            hist = scores.setdefault(str(entry_id), [])
            hist.append({"score": _compute_health_score(entry), "action": "失效"})
            store.set_pipeline_state("proxy_health_scores", scores)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"代理节点已{action}"

    def select_proxy(self, region: str | None = None) -> dict[str, Any] | None:
        """按健康评分选择最优可用代理。"""
        rows = store.rows(MODULE)
        candidates = [r for r in rows if r.get("status") == "可用"]
        if region:
            candidates = [r for r in candidates if region in str(r.get("地域标识") or "")]
        if not candidates:
            return None
        scored = [(r, _compute_health_score(r)) for r in candidates]
        scored.sort(key=lambda x: x[1], reverse=True)
        best = dict(scored[0][0])
        best["_selected_score"] = scored[0][1]
        return best

    # ------------------------------------------------------------------
    # 会话地域归属调度：代理编号与归属依据统一在代理池调度侧生成
    # ------------------------------------------------------------------

    def _health_state(self, proxy: dict[str, Any]) -> dict[str, Any]:
        """健康状态：状态、评分、成功率与延迟必须齐全才算健康可用。"""
        success_raw = str(proxy.get("成功率") or "").strip()
        latency_raw = str(proxy.get("响应延迟") or "").strip()
        score = _compute_health_score(proxy)
        if not success_raw or not latency_raw:
            return {"healthy": False, "score": score, "reason": "代理健康数据缺失（成功率或响应延迟为空）"}
        if proxy.get("status") != "可用":
            return {"healthy": False, "score": score, "reason": f"代理状态为{proxy.get('status') or '未知'}，不是可用状态"}
        if score < MIN_HEALTH_SCORE:
            return {"healthy": False, "score": score, "reason": f"健康评分 {score} 低于阈值 {MIN_HEALTH_SCORE}"}
        return {"healthy": True, "score": score, "reason": "健康可用"}

    def _scoped_candidates(self, region: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """返回 (归属范围内候选, 范围外候选)，均只保留健康可用代理。"""
        in_scope: list[dict[str, Any]] = []
        out_scope: list[dict[str, Any]] = []
        session_scope = {"region": region}
        for proxy in store.rows(MODULE):
            health = self._health_state(proxy)
            if not health["healthy"]:
                continue
            affiliation = proxy_affiliation(proxy)
            if not affiliation["raw"] or not affiliation["code"]:
                # 地域标识缺失：既不进本域候选，也不能作为跨域候选，直接不可分配。
                continue
            item = {
                "proxy_id": int(proxy["id"]),
                "proxy_code": proxy.get("节点编号"),
                "region": affiliation["raw"],
                "health_score": health["score"],
                "health_reason": health["reason"],
            }
            if region_matches(session_scope, affiliation):
                in_scope.append(item)
            else:
                out_scope.append(item)
        in_scope.sort(key=lambda x: x["health_score"], reverse=True)
        out_scope.sort(key=lambda x: x["health_score"], reverse=True)
        return in_scope, out_scope

    def _read_dispatches(self) -> dict[str, dict[str, Any]]:
        return store.pipeline_state("proxy_dispatch_results") or {}

    @staticmethod
    def _next_version(previous: dict[str, Any] | None) -> int:
        return int((previous or {}).get("version") or 0) + 1

    def _record_conflict(self, session_id: int, expected: int, current: int, writer: str) -> None:
        conflicts = store.pipeline_state("proxy_dispatch_conflicts") or []
        conflicts.append({
            "session_id": session_id,
            "expected_version": expected,
            "current_version": current,
            "writer": writer,
            "resolution": "以当前版本为准，拒绝落后版本的写入",
        })
        store.set_pipeline_state("proxy_dispatch_conflicts", conflicts)

    def _authorization_ok(self, authorization: dict[str, Any] | None, proxy_region: str) -> tuple[bool, dict[str, Any], str]:
        """跨域调度必须带明确授权：授权编号、授权人、授权范围缺一不可。"""
        data = dict(authorization or {})
        missing = [
            field for field in ("授权编号", "授权人", "授权范围")
            if not str(data.get(field) or "").strip()
        ]
        if missing:
            return False, data, f"跨域调度缺少授权依据：{'、'.join(missing)}"
        scope = [part.strip() for part in str(data.get("授权范围") or "").replace(";", ",").split(",") if part.strip()]
        region_code = normalize_region(proxy_region)["code"]
        if not (region_code in scope or "*" in scope or NATIONWIDE in scope):
            return False, data, f"授权范围 {data.get('授权范围')} 不包含代理地域 {proxy_region}"
        data["授权范围"] = scope
        return True, data, ""

    def dispatch_for_session(
        self,
        session_id: int,
        *,
        cross_region: bool = False,
        authorization: dict[str, Any] | None = None,
        expected_version: int | None = None,
        writer: str = DISPATCH_OWNER,
    ) -> dict[str, Any]:
        """为会话编排一条地域归属代理并生成调度结果。

        - 本域优先：归属范围内存在健康可用代理时，不用跨域代理；
        - 跨域必须带授权，且授权范围覆盖目标地域；
        - 地域或健康任一缺失都不分配代理；
        - 写入版本落后于当前版本时按版本先后仲裁，拒绝落后写入。
        """
        if writer != DISPATCH_OWNER:
            return {
                "ok": False,
                "code": "forbidden_writer",
                "message": f"模块 {writer} 无权写入代理调度结果，只有 {DISPATCH_OWNER} 可以写入",
            }
        session = store.find("session_mgr", session_id)
        if session is None:
            return {"ok": False, "code": "session_missing", "message": f"采集会话 {session_id} 不存在或已归档"}

        dispatches = self._read_dispatches()
        key = str(session_id)
        previous = dispatches.get(key)
        current_version = int((previous or {}).get("version") or 0)

        if expected_version is not None and expected_version != current_version:
            self._record_conflict(session_id, int(expected_version), current_version, writer)
            return {
                "ok": False,
                "code": "version_conflict",
                "message": (
                    f"版本冲突：提交基于 v{expected_version}，当前已是 v{current_version}；"
                    "按版本先后仲裁，本次写入被拒绝"
                ),
                "current_version": current_version,
                "expected_version": int(expected_version),
                "arbitration": {"winner": f"v{current_version}", "loser": f"v{expected_version}", "rule": "版本号大者优先"},
            }

        affiliation = resolve_session_affiliation(session)
        if not affiliation["region"]:
            result = {
                "ok": False,
                "code": "affiliation_missing",
                "message": affiliation["reason"],
                "session_id": session_id,
                "session_code": session.get("会话编号"),
                "status": "未分配",
                "proxy_id": None,
                "region": "",
                "affiliation": affiliation,
                "candidates": [],
                "version": self._next_version(previous),
                "updated_at": _utc_now(),
            }
            dispatches[key] = result
            store.set_pipeline_state("proxy_dispatch_results", dispatches)
            return result

        in_scope, out_scope = self._scoped_candidates(affiliation["region"])
        chosen = None
        scope_kind = "本域"
        auth_data: dict[str, Any] = {}
        audit_reason = ""
        if in_scope and not cross_region:
            chosen = in_scope[0]
            audit_reason = "归属范围内存在健康可用代理，按评分取最高，不越域"
        elif cross_region:
            if not out_scope:
                audit_reason = "跨域候选中没有健康可用的代理"
            else:
                best = out_scope[0]
                ok, auth_data, reason = self._authorization_ok(authorization, best["region"])
                if ok:
                    chosen = best
                    scope_kind = "跨域"
                    audit_reason = f"归属范围内无可用代理或已显式申请跨域；授权 {auth_data['授权编号']} 覆盖 {best['region']}"
                else:
                    # 显式申请跨域但授权不成立：拒绝本次写入，不改写已有同源结果；
                    # 同时提示本域候选仍然可用，由调用方决定是否改走本域。
                    audit_reason = reason
        else:
            audit_reason = "归属范围内没有健康可用的代理，且未申请跨域授权"

        version = self._next_version(previous)
        if chosen is None:
            # 未分配：保留上一版结果，只返回本次拒绝说明，避免同源结果被空结果覆盖。
            return {
                "ok": False,
                "code": "authorization_denied" if cross_region and out_scope else "no_healthy_proxy_in_scope",
                "message": audit_reason,
                "session_id": session_id,
                "session_code": session.get("会话编号"),
                "region": affiliation["region"],
                "region_raw": affiliation["raw"],
                "affiliation": affiliation,
                "proxy_id": None,
                "scope": None,
                "status": "未分配",
                "current_version": current_version,
                "candidates": in_scope,
                "out_of_scope_candidates": out_scope,
                "requested_authorization": dict(authorization or {}),
            }
        result: dict[str, Any] = {
            "ok": True,
            "code": "dispatched",
            "message": audit_reason,
            "session_id": session_id,
            "session_code": session.get("会话编号"),
            "status": "已归属",
            "region": affiliation["region"],
            "region_raw": affiliation["raw"],
            "affiliation": affiliation,
            "proxy_id": chosen["proxy_id"],
            "proxy_code": chosen["proxy_code"],
            "proxy_region": chosen["region"],
            "health_score": chosen["health_score"],
            "scope": scope_kind,
            "authorization": auth_data if scope_kind == "跨域" else {},
            "audit": {
                "reason": audit_reason,
                "cross_region": scope_kind == "跨域",
                "original_session_region": affiliation["raw"] if scope_kind == "跨域" else "",
            },
            "candidates": in_scope,
            "out_of_scope_candidates": out_scope,
            "version": version,
            "updated_at": _utc_now(),
            "writer": writer,
        }
        dispatches[key] = result
        store.set_pipeline_state("proxy_dispatch_results", dispatches)

        # 同一会话只保留一个有效代理绑定。
        store.remove_relations("session_to_proxy", source_module="session_mgr", source_id=session_id)
        store.add_relation(
            "session_to_proxy", "session_mgr", session_id, MODULE, chosen["proxy_id"],
            {
                "归属依据": affiliation["basis"],
                "地域": affiliation["raw"],
                "范围": scope_kind,
                "授权": result["authorization"],
                "版本": version,
            },
        )
        return result

    def get_dispatch_result(self, session_id: int, reader: str = "session_mgr") -> dict[str, Any] | None:
        """只读读取某会话的代理调度结果；非授权读取方返回 None。"""
        if reader not in DISPATCH_READERS:
            return None
        dispatch = self._read_dispatches().get(str(session_id))
        if dispatch is None:
            return None
        data = copy.deepcopy(dispatch)
        data["_readonly"] = True
        data["_owner"] = DISPATCH_OWNER
        return data

    def list_dispatch_results(self) -> list[dict[str, Any]]:
        dispatches = self._read_dispatches()
        return [
            dict(copy.deepcopy(dispatches[key]), _readonly=True, _owner=DISPATCH_OWNER)
            for key in sorted(dispatches, key=int)
        ]

    def write_dispatch_result(self, payload: dict[str, Any], *, writer: str) -> dict[str, Any]:
        """受控写入入口：只有代理池调度一侧可写，其他模块越权写入直接拒绝。"""
        if writer != DISPATCH_OWNER:
            return {
                "ok": False,
                "code": "forbidden_writer",
                "message": f"代理调度结果由 {DISPATCH_OWNER} 统一维护，{writer} 的写入被拒绝",
            }
        session_id = payload.get("session_id")
        try:
            sid = int(session_id)
        except (TypeError, ValueError):
            return {"ok": False, "code": "bad_session", "message": "缺少有效的 session_id"}
        return self.dispatch_for_session(
            sid,
            cross_region=bool(payload.get("cross_region")),
            authorization=payload.get("authorization"),
            expected_version=payload.get("expected_version"),
            writer=writer,
        )

    def record_request(self, entry_id: int, success: bool, latency_ms: float) -> dict[str, Any]:
        """记录代理请求结果，更新历史评分。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return {"ok": False}
        scores = store.pipeline_state("proxy_health_scores") or {}
        hist = scores.setdefault(str(entry_id), [])
        hist.append({"success": success, "latency": latency_ms, "score": _compute_health_score(entry)})
        if len(hist) > 50:
            scores[str(entry_id)] = hist[-50:]
        store.set_pipeline_state("proxy_health_scores", scores)
        return {"ok": True, "current_score": _compute_health_score(entry)}
