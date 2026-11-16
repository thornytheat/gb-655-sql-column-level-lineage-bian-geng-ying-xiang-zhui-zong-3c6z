"""有效边与发布视图：裁定只替换被裁定目标列的边，其余保持原解析。

原解析边与冲突证据不动，有效边是另一套并存的事实；上游变更沿有效边传播。
"""

from __future__ import annotations


def effective_edges(parse: dict, rulings: list[dict]) -> list[dict]:
    """原解析边应用生效裁定后的边集；撤销（active=False）即回到原解析。"""
    active = {ruling["target"]: ruling for ruling in rulings if ruling.get("active")}
    edges = [dict(edge) for edge in parse.get("edges") or []
             if edge["target"] not in active]
    for target, ruling in sorted(active.items()):
        for source in ruling["chosen_sources"]:
            edges.append({
                "source": source,
                "target": target,
                "line": ruling["chosen_line"],
                "ruled": True,
            })
    return sorted(edges, key=lambda edge: (edge["target"], edge["source"],
                                           edge.get("line") or 0))


def build_view(parse: dict, rulings: list[dict]) -> dict:
    """待发布的有效边视图；构建失败时调用方不得触碰旧版。"""
    edges = effective_edges(parse, rulings)
    return {
        "scope": parse["scope"],
        "effective_edges": edges,
        "edge_count": len(edges),
        "applied": [
            {
                "target": ruling["target"],
                "chosen_line": ruling["chosen_line"],
                "revision": ruling["revision"],
            }
            for ruling in sorted(rulings, key=lambda item: item["target"])
            if ruling.get("active")
        ],
    }
