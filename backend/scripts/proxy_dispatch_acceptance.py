"""华东会话代理调度验收脚本：本域低分代理 vs 跨域高分代理。

用法（在 backend 目录）：
    .venv/bin/python scripts/proxy_dispatch_acceptance.py

脚本只调用服务层，演示三条结果：本域归属、跨域授权、版本冲突。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.crawl_source import CrawlSourceService  # noqa: E402
from app.services.proxy_pool import ProxyPoolService, _compute_health_score  # noqa: E402
from app.services.session_mgr import SessionMgrService  # noqa: E402
from app.store import store  # noqa: E402


def dump(title: str, payload: dict) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    store.reset()
    source, _ = CrawlSourceService().create_entry({
        "源标识": "SRC-VERIFY-EAST",
        "源名称": "华东验收源",
        "协议类型": "https",
        "基础URL": "https://east.example.com/api",
        "地区归属": "华东-上海",
    })
    session_svc = SessionMgrService()
    session, _ = session_svc.create_entry({"会话编号": "SESS-VERIFY-EAST", "关联源": source["源标识"]})
    session_svc.run_action(session["id"], "标记活跃")

    proxy_svc = ProxyPoolService()
    # 隔离示例代理池，避免种子数据里的其他华东节点干扰演示取舍
    for seeded in store.rows("proxy_pool"):
        if seeded.get("status") != "已失效":
            proxy_svc.run_action(int(seeded["id"]), "标记失效")
    local, _ = proxy_svc.create_entry({
        "节点编号": "PROX-EAST-LOW", "代理地址": "10.1.1.1", "端口号": "8080",
        "地域标识": "华东-上海", "响应延迟": "300ms", "成功率": "90%", "失效次数": "1",
    })
    remote, _ = proxy_svc.create_entry({
        "节点编号": "PROX-NORTH-HIGH", "代理地址": "10.2.2.2", "端口号": "443",
        "地域标识": "华北-北京", "响应延迟": "20ms", "成功率": "100%", "失效次数": "0",
    })
    proxy_svc.run_action(local["id"], "标记可用")
    proxy_svc.run_action(remote["id"], "标记可用")

    print(f"本域代理 {local['节点编号']} 健康分：{_compute_health_score(local)}")
    print(f"跨域代理 {remote['节点编号']} 健康分：{_compute_health_score(remote)}")

    local_result = proxy_svc.dispatch_for_session(session["id"])
    dump("方案一：本域归属（牺牲评分，换取地域一致）", {
        "调度结果": local_result,
        "请求日志回写": session_svc.record_request(session["id"], "https://east.example.com/items", 210.0),
    })

    auth = {"授权编号": "AUTH-EAST-2026-01", "授权人": "值班调度", "授权范围": "华北"}
    # 当前结果已是 v1，这次跨域提交仍基于 v0（过期视图），按版本先后仲裁拒绝
    conflict = proxy_svc.dispatch_for_session(session["id"], cross_region=True,
                                              authorization=auth, expected_version=0)
    dump("版本冲突：跨域写入基于过期版本 v0", conflict)

    cross_result = proxy_svc.dispatch_for_session(session["id"], cross_region=True, authorization=auth)
    dump("方案二：跨域授权（提高评分，记录越域原因）", {
        "调度结果": cross_result,
        "请求日志回写": session_svc.record_request(session["id"], "https://east.example.com/items", 60.0),
    })

    dump("越权写入被拒绝", proxy_svc.write_dispatch_result(
        {"session_id": session["id"], "cross_region": True}, writer="session_mgr"))

    entry = session_svc.get_entry(session["id"])
    dump("会话请求记录（代理编号 + 归属依据）", {
        "绑定代理关系": store.get_relations("session_to_proxy", source_module="session_mgr", source_id=session["id"]),
        "请求日志": entry["_request_log"],
        "最新调度结果": entry["_dispatch"],
    })
    dump("版本冲突仲裁记录", {"items": store.pipeline_state("proxy_dispatch_conflicts")})


if __name__ == "__main__":
    main()
