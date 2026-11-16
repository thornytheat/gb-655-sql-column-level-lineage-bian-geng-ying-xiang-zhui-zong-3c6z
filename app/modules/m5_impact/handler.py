"""M5 入口：影响面页、影响面接口、列级血缘与目标列裁定。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import errors, store, web
from app.modules.m5_impact import rulings as rulings_mod

router = APIRouter()


@router.get("/impact", response_class=HTMLResponse)
def impact_page(request: Request):
    run = store.latest_run()
    payload = (run or {}).get("impact") or {}
    lineage = rulings_mod.state()
    return web.page_or_404(
        request,
        "impact.html",
        "impact",
        run,
        graph=payload.get("impact") or {},
        cycles=payload.get("cycles") or {},
        one_sided=payload.get("one_sided") or [],
        broken=payload.get("broken") or [],
        summary=payload.get("summary") or {},
        lineage=lineage,
        lineage_rows=rulings_mod.page_rows(lineage),
        phase_codes=rulings_mod.PHASE_CODES,
    )


@router.get("/api/impact")
def impact_api():
    run = store.latest_run()
    if not run:
        return {"run_id": None, "impact": {}, "summary": {}}
    payload = run.get("impact") or {}
    return {
        "run_id": run["run_id"],
        "impact": payload.get("impact") or {},
        "cycles": payload.get("cycles") or {},
        "one_sided": payload.get("one_sided") or [],
        "broken": payload.get("broken") or [],
        "summary": payload.get("summary") or {},
    }


@router.post("/api/lineage")
def lineage_api(payload: dict):
    """解析一批表达式并登记批次；同批重解析不抹掉已落库的裁定。"""
    body = payload or {}
    expressions = body.get("expressions") or []
    if not isinstance(expressions, list) or not all(isinstance(item, dict) for item in expressions):
        raise errors.BadInput("expressions 必须是对象列表", detail={"field": "expressions"})
    record = rulings_mod.record_parse(expressions, body.get("changed") or [])
    return rulings_mod.state(record["scope"])


@router.get("/api/lineage")
def lineage_state_api():
    return rulings_mod.state()


@router.post("/api/lineage/rulings")
def lineage_decide_api(payload: dict):
    ruling = rulings_mod.decide(payload or {})
    return {
        "scope": ruling["scope"],
        "target": ruling["target"],
        "chosen_line": ruling["chosen_line"],
        "revision": ruling["revision"],
        "active": ruling["active"],
        "pending": [item["target"]
                    for item in store.pending_rulings(ruling["scope"])],
    }


@router.post("/api/lineage/rulings/revoke")
def lineage_revoke_api(payload: dict):
    result = rulings_mod.revoke(payload or {})
    return {
        **result,
        "pending": [item["target"]
                    for item in store.pending_rulings(result["scope"])],
    }


@router.post("/api/lineage/publish")
def lineage_publish_api(payload: dict):
    """发布或续跑未发布部分；重试幂等，切换失败保留旧版。"""
    scope = str((payload or {}).get("scope") or "").strip()
    if not scope:
        raise errors.BadInput("缺少 scope", detail={"field": "scope"})
    return rulings_mod.publish(scope)
