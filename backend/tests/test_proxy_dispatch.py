"""会话地域归属代理调度规则测试。

覆盖：
- 代理地域与健康状态必须同时满足，任一缺失都不分配；
- 本域低分代理与跨域高分代理的取舍、跨域授权与审计；
- 调度结果版本冲突按先后仲裁，越权写入被拒绝；
- 会话请求日志只写入归属依据与代理编号，未授权跨域不落日志。
"""
from __future__ import annotations

import pytest

from app.services.crawl_source import CrawlSourceService
from app.services.proxy_pool import MODULE as PROXY_MODULE
from app.services.proxy_pool import ProxyPoolService, _compute_health_score
from app.services.region_scope import resolve_session_affiliation
from app.services.session_mgr import SessionMgrService
from app.store import store


@pytest.fixture()
def east_china_setup():
    """准备一个华东会话、本域低分代理与跨域高分代理。"""
    store.reset()
    src_svc = CrawlSourceService()

    # 隔离示例代理池：本用例只保留自己准备的本域/跨域两个候选，
    # 避免种子数据里同样落在华东的节点干扰取舍。
    proxy_svc = ProxyPoolService()
    for seeded in store.rows(PROXY_MODULE):
        if seeded.get("status") != "已失效":
            proxy_svc.run_action(int(seeded["id"]), "标记失效")
    src, _ = src_svc.create_entry({
        "源标识": "SRC-EAST-901",
        "源名称": "华东会话源",
        "协议类型": "https",
        "基础URL": "https://east.example.com/api",
        "地区归属": "华东-杭州",
    })
    sess_svc = SessionMgrService()
    sess, _ = sess_svc.create_entry({"会话编号": "SESS-EAST-901", "关联源": src["源标识"]})
    sess_svc.run_action(sess["id"], "标记活跃")

    local, _ = proxy_svc.create_entry({
        "节点编号": "PROX-EAST-LOW",
        "代理地址": "10.10.10.1",
        "端口号": "8080",
        "协议类型": "http",
        "地域标识": "华东-杭州",
        "响应延迟": "300ms",
        "成功率": "92.0%",
        "失效次数": "1",
    })
    remote, _ = proxy_svc.create_entry({
        "节点编号": "PROX-REMOTE-HIGH",
        "代理地址": "10.20.20.2",
        "端口号": "443",
        "协议类型": "https",
        "地域标识": "华北-北京",
        "响应延迟": "5ms",
        "成功率": "100%",
        "失效次数": "0",
    })
    proxy_svc.run_action(local["id"], "标记可用")
    proxy_svc.run_action(remote["id"], "标记可用")
    assert _compute_health_score(remote) > _compute_health_score(local)
    return {
        "src": src,
        "session": sess,
        "local": local,
        "remote": remote,
        "session_svc": sess_svc,
        "proxy_svc": proxy_svc,
    }


def test_affiliation_from_linked_source(east_china_setup) -> None:
    sess = store.find("session_mgr", east_china_setup["session"]["id"])
    affiliation = resolve_session_affiliation(sess)
    assert affiliation["region"] == "华东"
    assert "SRC-EAST-901" in affiliation["basis"]


def test_local_scope_prefers_region_over_global_score(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    result = svc.dispatch_for_session(east_china_setup["session"]["id"])

    assert result["ok"] is True
    assert result["scope"] == "本域"
    assert result["proxy_id"] == east_china_setup["local"]["id"]
    assert result["proxy_code"] == "PROX-EAST-LOW"
    assert result["health_score"] == pytest.approx(_compute_health_score(east_china_setup["local"]))
    # 全局最高分的跨域代理没有被使用，但候选集里能看到它
    assert any(item["proxy_code"] == "PROX-REMOTE-HIGH" for item in result["out_of_scope_candidates"])
    assert result["authorization"] == {}


def test_cross_region_requires_authorization(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    result = svc.dispatch_for_session(
        east_china_setup["session"]["id"], cross_region=True, authorization=None)
    assert result["ok"] is False
    assert result["code"] == "authorization_denied"
    assert result["proxy_id"] is None
    assert result["scope"] is None
    assert "授权" in result["message"]
    # 拒绝不落调度结果，也不产生代理绑定
    assert svc.get_dispatch_result(east_china_setup["session"]["id"]) is None
    assert store.get_relations("session_to_proxy", source_module="session_mgr",
                               source_id=east_china_setup["session"]["id"]) == []


def test_cross_region_with_authorization_audits_reason(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    auth = {"授权编号": "AUTH-2026-77", "授权人": "调度值班", "授权范围": "华北"}
    result = svc.dispatch_for_session(
        east_china_setup["session"]["id"], cross_region=True, authorization=auth)

    assert result["ok"] is True
    assert result["scope"] == "跨域"
    assert result["proxy_code"] == "PROX-REMOTE-HIGH"
    assert result["authorization"]["授权编号"] == "AUTH-2026-77"
    assert result["audit"]["cross_region"] is True
    assert result["audit"]["original_session_region"] == "华东-杭州"
    assert "授权 AUTH-2026-77" in result["audit"]["reason"]


def test_cross_region_authorization_scope_must_cover_proxy_region(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    auth = {"授权编号": "AUTH-2026-78", "授权人": "调度值班", "授权范围": "华南"}
    result = svc.dispatch_for_session(
        east_china_setup["session"]["id"], cross_region=True, authorization=auth)
    assert result["ok"] is False
    assert "不包含" in result["message"]


def test_region_and_health_must_both_hold(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    local_id = east_china_setup["local"]["id"]
    # 地域对但健康不达标：状态回退为待校验
    svc.run_action(local_id, "标记失效")
    result = svc.dispatch_for_session(east_china_setup["session"]["id"])
    assert result["ok"] is False
    assert result["proxy_id"] is None
    assert "没有健康可用" in result["message"]


def test_missing_proxy_region_is_not_assignable() -> None:
    from app.services.crawl_source import CrawlSourceService

    store.reset()
    svc = ProxyPoolService()
    for seeded in store.rows(PROXY_MODULE):
        if seeded.get("status") != "已失效":
            svc.run_action(int(seeded["id"]), "标记失效")

    src, _ = CrawlSourceService().create_entry({
        "源标识": "SRC-SCOPE-902", "源名称": "无地域代理测试", "协议类型": "https",
        "基础URL": "https://scope.example.com", "地区归属": "华东-杭州",
    })
    sess_svc = SessionMgrService()
    sess, _ = sess_svc.create_entry({"会话编号": "SESS-SCOPE-902", "关联源": src["源标识"]})
    proxy, _ = svc.create_entry({
        "节点编号": "PROX-NOREGION", "代理地址": "10.30.30.3", "端口号": "8080",
        "响应延迟": "50", "成功率": "99%",
    })
    svc.run_action(proxy["id"], "标记可用")
    result = svc.dispatch_for_session(sess["id"])
    assert result["ok"] is False
    assert result["proxy_id"] is None
    assert all(item["proxy_code"] != "PROX-NOREGION" for item in result["out_of_scope_candidates"])


def test_missing_session_affiliation_blocks_dispatch() -> None:
    store.reset()
    sess_svc = SessionMgrService()
    sess, _ = sess_svc.create_entry({"会话编号": "SESS-NOAFF-903", "关联源": "不存在的源"})
    result = ProxyPoolService().dispatch_for_session(sess["id"])
    assert result["ok"] is False
    assert result["code"] == "affiliation_missing"
    assert result["proxy_id"] is None


def test_version_conflict_is_arbitrated_by_version_order(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    session_id = east_china_setup["session"]["id"]

    first = svc.dispatch_for_session(session_id)
    assert first["version"] == 1
    second = svc.dispatch_for_session(session_id)
    assert second["version"] == 2

    stale = svc.dispatch_for_session(session_id, expected_version=1)
    assert stale["ok"] is False
    assert stale["code"] == "version_conflict"
    assert stale["current_version"] == 2
    assert stale["arbitration"]["winner"] == "v2"

    conflicts = store.pipeline_state("proxy_dispatch_conflicts")
    assert conflicts[-1]["session_id"] == session_id
    assert conflicts[-1]["expected_version"] == 1

    # 冲突不会改写已有结果，结果保持同源
    current = svc.get_dispatch_result(session_id)
    assert current["version"] == 2
    assert current["proxy_id"] == east_china_setup["local"]["id"]


def test_unauthorized_writer_is_rejected(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    session_id = east_china_setup["session"]["id"]
    svc.dispatch_for_session(session_id)

    denied = svc.write_dispatch_result(
        {"session_id": session_id, "cross_region": True},
        writer="session_mgr",
    )
    assert denied["ok"] is False
    assert denied["code"] == "forbidden_writer"

    result = svc.get_dispatch_result(session_id)
    assert result["proxy_id"] == east_china_setup["local"]["id"]
    assert result["scope"] == "本域"
    assert result["_readonly"] is True


def test_dispatch_result_is_readonly_shared(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    session_id = east_china_setup["session"]["id"]
    svc.dispatch_for_session(session_id)

    snapshot = svc.get_dispatch_result(session_id)
    snapshot["proxy_id"] = 99999
    snapshot["candidates"].clear()

    fresh = svc.get_dispatch_result(session_id)
    assert fresh["proxy_id"] == east_china_setup["local"]["id"]
    assert len(fresh["candidates"]) >= 1


def test_session_log_carries_proxy_and_affiliation(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    sess_svc = east_china_setup["session_svc"]
    session_id = east_china_setup["session"]["id"]
    svc.dispatch_for_session(session_id)

    logged = sess_svc.record_request(session_id, "https://east.example.com/items", 88.0)
    assert logged["ok"] is True
    assert logged["proxy_id"] == east_china_setup["local"]["id"]
    assert "SRC-EAST-901" in logged["affiliation_basis"]

    entry = sess_svc.get_entry(session_id)
    record = entry["_request_log"][-1]
    assert record["proxy_code"] == "PROX-EAST-LOW"
    assert record["session_region"] == "华东-杭州"
    assert record["scope"] == "本域"
    assert "SRC-EAST-901" in record["affiliation_basis"]


def test_session_log_rejected_without_dispatch() -> None:
    store.reset()
    sess_svc = SessionMgrService()
    sess, _ = sess_svc.create_entry({"会话编号": "SESS-NODISP-904", "关联源": "SRC-ELEC-001"})
    denied = sess_svc.record_request(sess["id"], "https://example.com/no-dispatch", 10.0)
    assert denied["ok"] is False
    assert denied["code"] == "no_dispatch"
    logs = store.pipeline_state("session_request_logs") or {}
    assert str(sess["id"]) not in logs


def test_session_log_rejected_when_cross_region_unauthorized(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    sess_svc = east_china_setup["session_svc"]
    session_id = east_china_setup["session"]["id"]
    svc.dispatch_for_session(session_id)

    # 人为把结果改成跨域但清空授权，模拟调度结果被篡改/降级
    dispatches = store.pipeline_state("proxy_dispatch_results")
    dispatches[str(session_id)]["scope"] = "跨域"
    dispatches[str(session_id)]["authorization"] = {}
    store.set_pipeline_state("proxy_dispatch_results", dispatches)

    denied = sess_svc.record_request(session_id, "https://east.example.com/cross", 70.0)
    assert denied["ok"] is False
    assert denied["code"] == "unauthorized_cross_region"
    logs = store.pipeline_state("session_request_logs") or {}
    assert logs.get(str(session_id)) is None


def test_session_log_writer_is_restricted() -> None:
    store.reset()
    sess_svc = SessionMgrService()
    sess, _ = sess_svc.create_entry({"会话编号": "SESS-WRITER-905", "关联源": "SRC-ELEC-001"})
    denied = sess_svc.record_request(sess["id"], "https://example.com", 10.0, writer="proxy_pool")
    assert denied["ok"] is False
    assert denied["code"] == "forbidden_writer"


def test_single_active_proxy_per_session(east_china_setup) -> None:
    svc = east_china_setup["proxy_svc"]
    session_id = east_china_setup["session"]["id"]
    svc.dispatch_for_session(session_id)
    svc.dispatch_for_session(
        session_id,
        cross_region=True,
        authorization={"授权编号": "AUTH-2026-90", "授权人": "调度值班", "授权范围": "华北,华东"},
    )

    rels = store.get_relations("session_to_proxy", source_module="session_mgr", source_id=session_id)
    assert len(rels) == 1
    assert rels[0]["target_id"] == east_china_setup["remote"]["id"]
    assert rels[0]["meta"]["范围"] == "跨域"
    assert rels[0]["meta"]["授权"]["授权编号"] == "AUTH-2026-90"


def test_missing_health_data_is_not_assignable() -> None:
    from app.services.crawl_source import CrawlSourceService

    store.reset()
    svc = ProxyPoolService()
    for seeded in store.rows(PROXY_MODULE):
        if seeded.get("status") != "已失效":
            svc.run_action(int(seeded["id"]), "标记失效")
    src, _ = CrawlSourceService().create_entry({
        "源标识": "SRC-EAST-906", "源名称": "健康数据缺失源", "协议类型": "https",
        "基础URL": "https://east.example.com", "地区归属": "华东-杭州",
    })
    sess_svc = SessionMgrService()
    sess, _ = sess_svc.create_entry({"会话编号": "SESS-EAST-906", "关联源": src["源标识"]})
    proxy, _ = svc.create_entry({
        "节点编号": "PROX-NOHEALTH", "代理地址": "10.40.40.4", "端口号": "8080",
        "地域标识": "华东-杭州", "响应延迟": "", "成功率": "",
    })
    svc.run_action(proxy["id"], "标记可用")
    result = svc.dispatch_for_session(sess["id"])
    assert result["ok"] is False
    assert all(item["proxy_code"] != "PROX-NOHEALTH" for item in result["candidates"])
