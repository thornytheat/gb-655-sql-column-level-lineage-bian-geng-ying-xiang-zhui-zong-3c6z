"""目标列裁定：两套来源并存、影响面纠正、撤销恢复、并发拒绝、断点续跑。

裁定只改有效边；原解析边与冲突证据按当时结果保留。
"""

from __future__ import annotations

import pytest

from app import store
from app.modules.m5_impact import rulings, sql_lineage, view as view_mod

EXPRESSIONS = [
    {"target": "dm.gross", "expression": "o.order_amt + o.tax_amt"},
    {"target": "dm.gross", "expression": "p.pay_amt + p.channel_fee"},
    {"target": "dm.net", "expression": "dm.gross - o.discount_amt"},
    {"target": "rpt.daily", "expression": "dm.net"},
]


def _parse(client, changed=None):
    return client.post("/api/lineage", json={
        "expressions": EXPRESSIONS,
        "changed": changed if changed is not None else ["o.order_amt"],
    })


def _decide(client, scope, **overrides):
    body = {"scope": scope, "target": "dm.gross", "chosen_line": 2,
            "reason": "复核确认支付口径为准", "base_revision": 0}
    body.update(overrides)
    return client.post("/api/lineage/rulings", json=body)


def test_first_arriving_expression_participates(client):
    """两套来源并存时，先到的表达式直接参与影响分析，后到的留冲突证据。"""
    body = _parse(client).json()
    assert body["scope"].startswith("lin-")
    gross_edges = [edge for edge in body["edges"] if edge["target"] == "dm.gross"]
    assert {edge["source"] for edge in gross_edges} == {"o.order_amt", "o.tax_amt"}
    assert body["conflicts"] == [{"line": 2, "target": "dm.gross",
                                  "reason": "TARGET_CONFLICT"}]
    # 候选里两套来源都留着：一套参与计算，一套等复核
    candidates = body["candidates"]["dm.gross"]
    assert [item["status"] for item in candidates] == ["active", "conflict"]
    assert body["affected"] == ["dm.gross", "dm.net", "rpt.daily"]


def test_decide_corrects_impact_but_keeps_both_facts(client):
    """采信另一套来源后影响面随之纠正；原解析边与冲突证据原样保留。"""
    scope = _parse(client).json()["scope"]
    decided = _decide(client, scope)
    assert decided.status_code == 200
    assert decided.json()["revision"] == 1
    assert "dm.gross" in decided.json()["pending"]

    body = client.get("/api/lineage").json()
    # 有效边换成采信的来源，影响沿有效边传播
    effective = {edge["source"] for edge in body["effective_edges"]
                 if edge["target"] == "dm.gross"}
    assert effective == {"p.pay_amt", "p.channel_fee"}
    assert body["affected"] == []
    # 原解析边与冲突证据不动，两套事实并存
    original = {edge["source"] for edge in body["edges"] if edge["target"] == "dm.gross"}
    assert original == {"o.order_amt", "o.tax_amt"}
    assert body["affected_original"] == ["dm.gross", "dm.net", "rpt.daily"]
    assert body["conflicts"][0]["reason"] == "TARGET_CONFLICT"
    # 变更列沿有效边仍能到达下游
    other = _parse(client, changed=["p.pay_amt"]).json()
    assert other["affected"] == ["dm.gross", "dm.net", "rpt.daily"]


def test_revoke_restores_original_parse(client):
    """撤销裁定即恢复原解析。"""
    scope = _parse(client).json()["scope"]
    _decide(client, scope)
    revoked = client.post("/api/lineage/rulings/revoke", json={
        "scope": scope, "target": "dm.gross", "reason": "复核意见撤回",
        "base_revision": 1,
    })
    assert revoked.status_code == 200
    assert revoked.json()["active"] is False
    body = client.get("/api/lineage").json()
    effective = {edge["source"] for edge in body["effective_edges"]
                 if edge["target"] == "dm.gross"}
    assert effective == {"o.order_amt", "o.tax_amt"}
    assert body["affected"] == ["dm.gross", "dm.net", "rpt.daily"]


def test_concurrent_decide_rejects_stale_revision(client):
    """并发裁定：后到者带着过期修订号，必须 409。"""
    scope = _parse(client).json()["scope"]
    first = _decide(client, scope)
    assert first.status_code == 200
    stale = _decide(client, scope, reason="并发的另一裁定", chosen_line=1)
    assert stale.status_code == 409
    assert stale.json()["error"] == "RULING_STALE"
    assert stale.json()["detail"]["current_revision"] == 1
    # 用最新修订号重提才受理
    accepted = _decide(client, scope, reason="按最新状态改采原解析",
                       chosen_line=1, base_revision=1)
    assert accepted.status_code == 200
    assert accepted.json()["revision"] == 2


def test_revoke_requires_current_revision(client):
    scope = _parse(client).json()["scope"]
    _decide(client, scope)
    stale = client.post("/api/lineage/rulings/revoke", json={
        "scope": scope, "target": "dm.gross", "reason": "过期撤销", "base_revision": 0,
    })
    assert stale.status_code == 409
    ok = client.post("/api/lineage/rulings/revoke", json={
        "scope": scope, "target": "dm.gross", "reason": "正常撤销", "base_revision": 1,
    })
    assert ok.status_code == 200


def test_reason_is_required_and_recorded(client):
    """裁定与撤销都必须记录理由，理由随事件可查。"""
    scope = _parse(client).json()["scope"]
    missing = _decide(client, scope, reason="  ")
    assert missing.status_code == 400
    _decide(client, scope)
    events = client.get("/api/lineage").json()["events"]
    decided = [event for event in events if event["phase"] == "decided"]
    assert decided and decided[0]["reason"] == "复核确认支付口径为准"


def test_publish_resume_after_interruption(client, monkeypatch):
    """决定落库后发布中断：旧版保留，续跑只补未发布部分。"""
    scope = _parse(client).json()["scope"]
    client.post("/api/lineage/publish", json={"scope": scope})
    assert store.load_lineage_view(scope)["view_seq"] == 1

    _decide(client, scope)
    assert store.pending_rulings(scope)  # 决定已落库，发布未发生

    def boom(parse, ruling_list):
        raise RuntimeError("模拟发布中断")

    with monkeypatch.context() as patch:
        patch.setattr(view_mod, "build_view", boom)
        with pytest.raises(RuntimeError):
            rulings.publish(scope)
    # 切换失败：旧版视图原样保留，裁定仍是待发布
    assert store.load_lineage_view(scope)["view_seq"] == 1
    assert [r["target"] for r in store.pending_rulings(scope)] == ["dm.gross"]

    resumed = client.post("/api/lineage/publish", json={"scope": scope})
    assert resumed.status_code == 200
    assert resumed.json()["published"] == ["dm.gross"]
    assert resumed.json()["view_seq"] == 2
    assert store.pending_rulings(scope) == []
    view = store.load_lineage_view(scope)
    assert view["applied"] == [{"target": "dm.gross", "chosen_line": 2, "revision": 1}]


def test_retry_publish_does_not_register_twice(client):
    """发布重试不重复登记：事件按内容定主键，命中即忽略。"""
    scope = _parse(client).json()["scope"]
    _decide(client, scope)
    first = client.post("/api/lineage/publish", json={"scope": scope}).json()
    assert first["published"] == ["dm.gross"]
    count = store.count_ruling_events(scope)
    again = client.post("/api/lineage/publish", json={"scope": scope}).json()
    assert again["skipped"] is True
    assert again["view_seq"] == first["view_seq"]
    assert store.count_ruling_events(scope) == count


def test_reparse_same_batch_keeps_rulings(client):
    """重新解析同批表达式：解析记录不改写，裁定不抹掉。"""
    scope = _parse(client).json()["scope"]
    _decide(client, scope)
    again = _parse(client)
    assert again.status_code == 200
    assert again.json()["scope"] == scope
    ruling = store.load_ruling(scope, "dm.gross")
    assert ruling["active"] is True
    assert ruling["chosen_line"] == 2
    effective = {edge["source"] for edge in again.json()["effective_edges"]
                 if edge["target"] == "dm.gross"}
    assert effective == {"p.pay_amt", "p.channel_fee"}


def test_decide_validation(client):
    scope = _parse(client).json()["scope"]
    unknown_scope = _decide(client, "lin-missing")
    assert unknown_scope.status_code == 404
    no_conflict = client.post("/api/lineage/rulings", json={
        "scope": scope, "target": "dm.net", "chosen_line": 3,
        "reason": "单来源无需裁定", "base_revision": 0,
    })
    assert no_conflict.status_code == 400
    bad_line = _decide(client, scope, chosen_line=99)
    assert bad_line.status_code == 400


def test_seeded_lineage_visible_on_impact_page(client):
    """种子批次自带解析：影响面页能对照原解析与有效来源。"""
    body = client.get("/api/lineage").json()
    assert body["scope"]
    assert body["conflict_count"] == 1
    assert body["view"]["view_seq"] == 1
    page = client.get("/impact")
    assert page.status_code == 200
    assert "目标列裁定" in page.text
    assert "dm.gross" in page.text
    assert "p.pay_amt" in page.text
    assert "o.order_amt" in page.text


def test_scope_is_content_addressed():
    left = sql_lineage.scope_of(EXPRESSIONS)
    right = sql_lineage.scope_of([dict(item) for item in EXPRESSIONS])
    assert left == right
    changed = sql_lineage.scope_of(EXPRESSIONS + [
        {"target": "rpt.extra", "expression": "dm.net"},
    ])
    assert changed != left
