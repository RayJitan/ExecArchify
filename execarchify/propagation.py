"""Follow the impact of a changed entity within one indexed snapshot."""

from collections import defaultdict
from dataclasses import dataclass
from itertools import chain


_MAX_EDGES = 8192
_MAX_PATH_ENTRIES = 32768


@dataclass(slots=True)
class _Visit:
    node: dict
    parent: "_Visit | None"
    edge: dict | None
    # Fewer unknown, inferred, then heuristic hops wins at the same distance.
    quality: tuple[int, int, int]

    def evidence(self) -> list[dict]:
        path = []
        visit = self
        while visit.edge is not None:
            path.append(visit.edge)
            visit = visit.parent
        path.reverse()
        return path


def _quality(previous, confidence):
    unknown, inferred, heuristic = previous
    if confidence == "inferred":
        inferred += 1
    elif confidence == "heuristic":
        heuristic += 1
    elif confidence not in {"parsed", "declared"}:
        unknown += 1
    return unknown, inferred, heuristic


def _tie(edge, parent):
    return (parent.node["id"], edge.get("repo", ""), edge.get("path", ""),
            edge.get("line", 0), edge.get("kind", ""), edge.get("pointer", ""))


def propagate(view, start_node, *, depth: int, limit: int, gaps: set) -> tuple[list[tuple[dict, list[dict]]], bool]:
    """Return each reachable dependent once, with its shortest evidenced path.

    The requested depth is the search boundary. Truncation indicates that a
    work or result budget cut into that boundary. Every edge keeps its original
    direction, including when following a callee back to its caller.
    """
    if depth <= 0:
        return [], False
    limit = max(0, limit)
    budget = min(_MAX_EDGES, max(256, limit * 32))
    initial_gaps = gaps.copy()
    truncated = False
    forward = defaultdict(list)
    for position, edge in enumerate(view.declared()):
        if position == budget:
            truncated = True
            gaps.add("Impact declaration scan budget reached")
            break
        if edge.get("impact_direction", "reverse") in {"forward", "both"}:
            forward[edge["source"]].append(edge)

    frontier = [_Visit(start_node, None, None, (0, 0, 0))]
    seen = {start_node["id"]}
    missing = set()
    results = []
    examined = 0
    path_entries = 0
    for distance in range(1, depth + 1):
        next_level = {}
        work_exhausted = False
        for current in frontier:
            current_id = current.node["id"]
            incoming = ((edge, False) for edge in view.neighbors(current.node, "in", gaps))
            outgoing = ((edge, True) for edge in forward.get(current_id, ()))
            for edge, goes_forward in chain(incoming, outgoing):
                if examined == budget:
                    work_exhausted = True
                    truncated = True
                    gaps.add("Impact edge scan budget reached")
                    break
                examined += 1
                if edge.get("kind") == "dynamic_call" or edge.get("confidence") in {"unresolved", "unknown"}:
                    continue
                if goes_forward:
                    other = edge.get("target")
                else:
                    if edge.get("target") != current_id:
                        continue
                    if edge.get("confidence") == "declared" and edge.get("impact_direction", "reverse") not in {"reverse", "both"}:
                        continue
                    other = edge.get("source")
                if not other or other in seen or other in missing:
                    continue
                quality = _quality(current.quality, edge.get("confidence"))
                prior = next_level.get(other)
                if prior is not None:
                    if quality > prior.quality or (quality == prior.quality and _tie(edge, current) >= _tie(prior.edge, prior.parent)):
                        continue
                    node = prior.node
                else:
                    node = view.node(other)
                    if node is None:
                        missing.add(other)
                        continue
                next_level[other] = _Visit(node, current, edge, quality)
            if work_exhausted:
                break
        # Settle the whole layer before expanding it. An early weak route to
        # an intermediate node can otherwise taint every downstream path.
        frontier = sorted(next_level.values(), key=lambda visit: (visit.quality, visit.node["id"]))
        for visit in frontier:
            if len(results) == limit:
                gaps.add("Impact result budget reached")
                return results, True
            if path_entries + distance > _MAX_PATH_ENTRIES:
                gaps.add("Impact evidence path budget reached")
                return results, True
            seen.add(visit.node["id"])
            results.append((visit.node, visit.evidence()))
            path_entries += distance
        if not frontier or work_exhausted:
            break
    # View.neighbors can independently stop at its per-symbol reference cap.
    return results, truncated or any("budget" in gap.lower() for gap in gaps - initial_gaps)
