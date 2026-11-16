"""列级 SQL 血缘：别名解析、表达式源列与反向影响。"""

from __future__ import annotations

import hashlib
import json
import re

FIELD = re.compile(r"\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\b")
TARGET = re.compile(r"\s+AS\s+([A-Za-z_][\w]*)\s*$", re.IGNORECASE)


def scope_of(expressions: list[dict]) -> str:
    """同一批表达式永远得到同一个 scope：裁定挂在内容上，同批重解析不丢。"""
    payload = json.dumps(expressions, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return "lin-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def extract_edges(expressions: list[dict]) -> dict:
    edges: list[dict] = []
    conflicts: list[dict] = []
    by_target: dict[str, tuple[str, ...]] = {}
    candidates: dict[str, list[dict]] = {}
    for line, item in enumerate(expressions, 1):
        target = str(item.get("target") or "").strip().lower()
        expression = str(item.get("expression") or "")
        if not target:
            match = TARGET.search(expression)
            target = match.group(1).lower() if match else ""
        sources = tuple(sorted({f"{alias.lower()}.{field.lower()}" for alias, field in FIELD.findall(expression)}))
        if not target or not sources:
            conflicts.append({"line": line, "target": target or "?", "reason": "LINEAGE_EMPTY"})
            continue
        bucket = candidates.setdefault(target, [])
        if target in by_target and by_target[target] != sources:
            # 后到的另一套来源不进边，只留候选与冲突证据，等复核裁定
            conflicts.append({"line": line, "target": target, "reason": "TARGET_CONFLICT"})
            bucket.append({"line": line, "sources": list(sources), "status": "conflict"})
            continue
        bucket.append({"line": line, "sources": list(sources),
                       "status": "active" if target not in by_target else "duplicate"})
        by_target[target] = sources
        edges.extend({"source": source, "target": target, "line": line} for source in sources)
    return {"edges": edges, "conflicts": conflicts, "targets": len(by_target),
            "candidates": candidates}


def reverse_impact(edges: list[dict], changed: list[str]) -> list[str]:
    reverse: dict[str, set[str]] = {}
    for edge in edges:
        reverse.setdefault(str(edge["source"]), set()).add(str(edge["target"]))
    changed_set = set(map(str, changed))
    seen = set(changed_set)
    pending = list(sorted(seen))
    while pending:
        node = pending.pop(0)
        for target in sorted(reverse.get(node, set())):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    return sorted(seen - changed_set)
