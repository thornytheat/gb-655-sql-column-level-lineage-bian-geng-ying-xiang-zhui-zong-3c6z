"""目标列裁定：采信备选来源、撤销恢复、两阶段发布。

裁定只改"有效边"，原解析边与冲突证据按当时结果保留，两套事实并存。
决定先落库（带修订号乐观锁与审计事件），再发布成视图；发布可中断、
可续跑、重试不重复登记，切换失败保留旧版。
"""

from __future__ import annotations

import hashlib

from app import config, errors, store
from app.modules.m5_impact import sql_lineage
from app.modules.m5_impact import view as view_mod

MAX_REASON = 200
PHASE_CODES = {
    "decided": "RULING_DECIDED",
    "revoked": "RULING_REVOKED",
    "published": "RULING_PUBLISHED",
}


def _event(scope: str, target: str, revision: int, phase: str, detail: dict) -> dict:
    """事件 id 由内容推导：同一决定的同一阶段重试必然得到同一 id。"""
    key = f"{scope}|{target}|{revision}|{phase}"
    return {
        "event_id": "ev-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
        "scope": scope,
        "target": target,
        "revision": revision,
        "phase": phase,
        "created_at": config.now_utc().isoformat(),
        "detail": detail,
    }


def _check_reason(reason) -> str:
    text = str(reason or "").strip()
    if not text:
        raise errors.BadInput("裁定与撤销都必须记录理由", detail={"field": "reason"})
    if len(text) > MAX_REASON:
        raise errors.BadInput("理由最长 200 字",
                             detail={"field": "reason", "count": len(text)})
    return text


def _check_ids(payload: dict) -> tuple[str, str]:
    scope = str(payload.get("scope") or "").strip()
    target = str(payload.get("target") or "").strip().lower()
    if not scope or not target:
        raise errors.BadInput("缺少 scope 或 target",
                             detail={"fields": ["scope", "target"]})
    return scope, target


def _check_int(payload: dict, field: str, default=None) -> int:
    raw = payload.get(field, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise errors.BadInput(f"{field} 必须是整数", detail={"field": field,
                                                             "value": repr(raw)})


def _stale(base_revision: int, current_revision: int) -> errors.RulingStale:
    return errors.RulingStale(
        f"裁定基于修订号 {base_revision}，当前已是 {current_revision}",
        detail={"base_revision": base_revision, "current_revision": current_revision,
                "reason": "RULING_STALE"},
    )


def _candidates_for(parse: dict, target: str) -> list[dict]:
    candidates = (parse.get("candidates") or {}).get(target) or []
    distinct = {tuple(item["sources"]) for item in candidates}
    if len(distinct) < 2:
        raise errors.BadInput("该目标列没有两套来源，无需裁定",
                             detail={"target": target})
    return candidates


def record_parse(expressions: list[dict], changed: list[str]) -> dict:
    """解析并登记批次；同批重解析不改写，已落库的裁定自然保留。"""
    scope = sql_lineage.scope_of(expressions)
    record = store.load_lineage_parse(scope)
    if record is None:
        parsed = sql_lineage.extract_edges(expressions)
        record = {
            "scope": scope,
            "created_at": config.now_utc().isoformat(),
            "expr_count": len(expressions),
            "edge_count": len(parsed["edges"]),
            "conflict_count": len(parsed["conflicts"]),
            "expressions": expressions,
            **parsed,
        }
        store.save_lineage_parse(record)
    store.set_kv("lineage_current_scope", scope)
    store.set_kv("lineage_changed", [str(item) for item in (changed or [])])
    return record


def decide(payload: dict) -> dict:
    """采信一套候选来源：决定落库并登记审计事件，随后由发布生效。"""
    scope, target = _check_ids(payload)
    reason = _check_reason(payload.get("reason"))
    chosen_line = _check_int(payload, "chosen_line")
    base_revision = _check_int(payload, "base_revision", 0)

    parse = store.load_lineage_parse(scope)
    if parse is None:
        raise errors.NotFound("解析批次不存在", detail={"scope": scope})
    candidates = _candidates_for(parse, target)
    chosen = next((item for item in candidates if item["line"] == chosen_line), None)
    if chosen is None:
        raise errors.BadInput("所选行不在该目标列的候选来源里",
                             detail={"target": target, "chosen_line": chosen_line})

    current = store.load_ruling(scope, target)
    expected = current["revision"] if current else 0
    if base_revision != expected:
        raise _stale(base_revision, expected)

    now = config.now_utc().isoformat()
    ruling = {
        "scope": scope,
        "target": target,
        "chosen_line": chosen_line,
        "chosen_sources": list(chosen["sources"]),
        "reason": reason,
        "revision": expected + 1,
        "active": True,
        "published_revision": current["published_revision"] if current else 0,
        "decided_at": now,
        "updated_at": now,
    }
    event = _event(scope, target, ruling["revision"], "decided",
                   {"reason": reason, "chosen_line": chosen_line,
                    "chosen_sources": list(chosen["sources"])})
    if not store.ruling_decide(ruling, event, expected):
        raise _stale(base_revision, expected)
    return ruling


def revoke(payload: dict) -> dict:
    """撤销裁定：有效边回到原解析，决定同样落库并等发布。"""
    scope, target = _check_ids(payload)
    reason = _check_reason(payload.get("reason"))
    base_revision = _check_int(payload, "base_revision", 0)

    current = store.load_ruling(scope, target)
    if current is None:
        raise errors.NotFound("该目标列没有裁定", detail={"scope": scope, "target": target})
    if not current["active"]:
        raise errors.BadInput("该目标列的裁定已撤销", detail={"target": target})
    if base_revision != current["revision"]:
        raise _stale(base_revision, current["revision"])

    revision = current["revision"] + 1
    event = _event(scope, target, revision, "revoked", {"reason": reason})
    if not store.ruling_revoke(scope, target, reason=reason, revision=revision,
                               expected_revision=current["revision"],
                               updated_at=config.now_utc().isoformat(), event=event):
        raise _stale(base_revision, current["revision"])
    return {"scope": scope, "target": target, "revision": revision, "active": False}


def publish(scope: str) -> dict:
    """发布未发布部分：先构建新视图再单事务切换；构建失败旧版原样保留。"""
    parse = store.load_lineage_parse(scope)
    if parse is None:
        raise errors.NotFound("解析批次不存在", detail={"scope": scope})
    pending = store.pending_rulings(scope)
    existing = store.load_lineage_view(scope)
    if not pending and existing is not None:
        return {"scope": scope, "view_seq": existing["view_seq"], "published": [],
                "skipped": True}
    payload = view_mod.build_view(parse, store.list_rulings(scope))
    payload["published_at"] = config.now_utc().isoformat()
    events = [
        _event(scope, ruling["target"], ruling["revision"], "published",
               {"reason": ruling["reason"], "chosen_line": ruling["chosen_line"],
                "active": ruling["active"]})
        for ruling in pending
    ]
    seq = store.apply_publication(scope, events, pending, payload)
    return {"scope": scope, "view_seq": seq,
            "published": [ruling["target"] for ruling in pending], "skipped": False}


def state(scope: str | None = None) -> dict:
    """当前批次全貌：原解析、裁定、有效边、沿有效边的影响、发布状态。"""
    if scope is None:
        scope = store.get_kv("lineage_current_scope")
    parse = store.load_lineage_parse(scope) if scope else None
    if parse is None:
        return {"scope": None, "edges": [], "conflicts": [], "candidates": {},
                "rulings": [], "effective_edges": [], "affected": [],
                "affected_original": [], "pending": [], "events": [], "view": None}
    rulings = store.list_rulings(scope)
    changed = store.get_kv("lineage_changed", [])
    effective = view_mod.effective_edges(parse, rulings)
    return {
        "scope": scope,
        "expr_count": parse["expr_count"],
        "edge_count": parse["edge_count"],
        "conflict_count": parse["conflict_count"],
        "targets": parse["targets"],
        "edges": parse["edges"],
        "conflicts": parse["conflicts"],
        "candidates": parse["candidates"],
        "rulings": rulings,
        "effective_edges": effective,
        "changed": changed,
        "affected": sql_lineage.reverse_impact(effective, changed),
        "affected_original": sql_lineage.reverse_impact(parse["edges"], changed),
        "pending": [ruling["target"] for ruling in store.pending_rulings(scope)],
        "events": store.list_ruling_events(scope, limit=20),
        "view": store.load_lineage_view(scope),
    }


def page_rows(st: dict) -> list[dict]:
    """模板行：每目标列的原解析、备选、当前有效与裁定，便于对照。"""
    by_target = {ruling["target"]: ruling for ruling in st.get("rulings") or []}
    rows = []
    for target in sorted(st.get("candidates") or {}):
        candidates = st["candidates"][target]
        if len({tuple(item["sources"]) for item in candidates}) < 2:
            continue
        original = candidates[0]
        seen = {tuple(original["sources"])}
        alternatives = []
        for item in candidates[1:]:
            key = tuple(item["sources"])
            if key in seen:
                continue
            seen.add(key)
            alternatives.append(item)
        ruling = by_target.get(target)
        active = ruling if ruling and ruling["active"] else None
        rows.append({
            "target": target,
            "original": original,
            "alternatives": alternatives,
            "ruling": ruling,
            "effective": ({"line": active["chosen_line"], "sources": active["chosen_sources"]}
                          if active else original),
            "corrected": bool(active)
                         and tuple(active["chosen_sources"]) != tuple(original["sources"]),
        })
    return rows
