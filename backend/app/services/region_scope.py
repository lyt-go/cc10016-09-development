"""地域归属解析：代理节点地域、会话归属与调度范围判定。

本模块只做只读解析，不写任何业务数据。代理调度结果由代理池调度侧统一生成，
会话管理只能引用这里解析出的归属依据，不能反向修改代理地域或健康状态。
"""
from __future__ import annotations

from typing import Any

from app.store import store

KNOWN_REGIONS = ("华东", "华北", "华南", "海外")
NATIONWIDE = "全国"
_SEPARATORS = ("-", "－", "—", "/", " ")


def normalize_region(value: Any) -> dict[str, str]:
    """把「华东-杭州」这类地域标识拆成地域编码与城市。

    无法识别的写法整体作为地域编码返回，保证不会把未知地域误判成本域。
    """
    raw = str(value or "").strip()
    if not raw:
        return {"raw": "", "code": "", "city": ""}
    for separator in _SEPARATORS:
        if separator in raw:
            head, _, tail = raw.partition(separator)
            head = head.strip()
            if head:
                return {"raw": raw, "code": head, "city": tail.strip()}
    return {"raw": raw, "code": raw, "city": ""}


def proxy_affiliation(proxy: dict[str, Any]) -> dict[str, str]:
    """读取代理节点地域归属，缺失时 code 为空。"""
    return normalize_region(proxy.get("地域标识"))


def _find_session_source(session: dict[str, Any]) -> dict[str, Any] | None:
    """会话的「关联源」既可能是源标识，也可能是数值 id，两种都接受。"""
    source_ref = str(session.get("关联源") or "").strip()
    if not source_ref:
        return None
    source = store.find_by_field("crawl_source", "源标识", source_ref)
    if source is not None:
        return source
    try:
        return store.find("crawl_source", int(source_ref))
    except (TypeError, ValueError):
        return None


def resolve_session_affiliation(session: dict[str, Any]) -> dict[str, Any]:
    """解析会话调度归属。

    优先使用会话自身显式登记的地域，其次读取关联采集源的「地区归属」。
    两者都缺失时返回空地域，调用方必须据此拒绝代理分配。
    """
    explicit = str(session.get("归属地域") or "").strip()
    source = _find_session_source(session)
    raw = explicit or str((source or {}).get("地区归属") or "").strip()
    region = normalize_region(raw)
    source_code = str((source or {}).get("源标识") or session.get("关联源") or "").strip()
    if not region["code"]:
        return {
            "region": "",
            "raw": "",
            "city": "",
            "source_id": int(source["id"]) if source else None,
            "source_code": source_code,
            "basis": "",
            "reason": "会话归属地域缺失：既未登记归属地域，关联采集源也没有地区归属，不能分配代理",
        }
    if explicit:
        basis = f"会话显式归属 {region['raw']}"
    else:
        basis = f"会话关联源 {source_code or '未知'} 地区归属 {region['raw']}"
    return {
        "region": region["code"],
        "raw": region["raw"],
        "city": region["city"],
        "source_id": int(source["id"]) if source else None,
        "source_code": source_code,
        "basis": basis,
        "reason": "",
    }


def region_matches(session_affiliation: dict[str, Any], proxy_affiliation_data: dict[str, Any]) -> bool:
    """判断代理地域是否落在会话归属范围内。

    「全国」源视为可在境内地域内就近调度，但仍不能落到海外节点。
    """
    session_region = str(session_affiliation.get("region") or session_affiliation.get("code") or "")
    proxy_region = str(proxy_affiliation_data.get("region") or proxy_affiliation_data.get("code") or "")
    if not session_region or not proxy_region:
        return False
    if session_region == proxy_region:
        return True
    if session_region == NATIONWIDE and proxy_region in KNOWN_REGIONS and proxy_region != "海外":
        return True
    return False
