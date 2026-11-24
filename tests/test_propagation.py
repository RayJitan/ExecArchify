import unittest

from execarchify.propagation import propagate


def edge(source, target, kind="call", confidence="parsed", **details):
    return dict(source=source, target=target, kind=kind, confidence=confidence,
                repo="root", path="code.py", line=7, **details)


class Graph:
    def __init__(self, ids, *edges):
        self.nodes = {key: {"id": key, "name": key, "path": key} for key in ids}
        self.edges = edges
        self.expanded = []

    def declared(self):
        return (e for e in self.edges if e["confidence"] == "declared")

    def neighbors(self, node, direction, gaps):
        assert direction == "in"
        self.expanded.append(node["id"])
        return (e for e in self.edges if e["target"] == node["id"])

    def node(self, ident):
        return self.nodes.get(ident)

    def impacts(self, start, depth=5, limit=20):
        gaps = set()
        results, truncated = propagate(self, self.node(start), depth=depth, limit=limit, gaps=gaps)
        return {node["id"]: path for node, path in results}, truncated, gaps


class PropagationTests(unittest.TestCase):
    def test_declared_forward_then_reverse_call_keeps_original_evidence(self):
        emit = edge("old#publish", "old#event", "writes", "declared", impact_direction="forward", pointer="/relations/0")
        read = edge("old#event", "old#read", "feeds", "declared", impact_direction="forward", pointer="/relations/1")
        use = edge("old#caller", "old#read")
        graph = Graph(["old#publish", "old#event", "old#read", "old#caller"], emit, read, use)
        hits, truncated, gaps = graph.impacts("old#publish")
        self.assertEqual(hits, {"old#event": [emit], "old#read": [emit, read], "old#caller": [emit, read, use]})
        self.assertFalse(truncated)
        self.assertEqual(gaps, set())
        self.assertEqual(graph.impacts("old#caller")[0], {})

    def test_reverse_default_both_none_and_cycles(self):
        connects = edge("b", "c", "connects", "declared", impact_direction="both")
        reverse = edge("a", "b", "owns", "declared")
        graph = Graph(["a", "b", "c", "skip", "cycle"], reverse, connects,
                      edge("c", "skip", "note", "declared", impact_direction="none"),
                      edge("skip", "c", "note", "declared", impact_direction="none"),
                      edge("c", "cycle", "next", "declared", impact_direction="forward"),
                      edge("cycle", "b", "next", "declared", impact_direction="forward"))
        hits, truncated, _ = graph.impacts("c")
        self.assertEqual(set(hits), {"a", "b", "cycle"})
        self.assertEqual(hits["a"], [connects, reverse])
        self.assertFalse(truncated)
        self.assertEqual(len(graph.expanded), len(set(graph.expanded)))
        self.assertEqual(graph.impacts("a")[0], {})
        self.assertIn("c", graph.impacts("b")[0])

    def test_confidence_selects_equal_length_path_before_expanding_descendants(self):
        a = edge("a", "seed")
        b = edge("b", "seed")
        weak = edge("merge", "a", confidence="inferred")
        strong = edge("merge", "b")
        last = edge("end", "merge")
        short = edge("short", "seed", confidence="heuristic")
        graph = Graph(["seed", "a", "b", "merge", "end", "short"], a, b, weak, strong, last, short, edge("short", "a"))
        hits, truncated, _ = graph.impacts("seed")
        self.assertFalse(truncated)
        self.assertEqual(hits["merge"], [b, strong])
        self.assertEqual(hits["end"], [b, strong, last])
        self.assertEqual(hits["short"], [short])

    def test_depth_limit_node_budget_and_unavailable_targets(self):
        graph = Graph(["a", "b", "c", "d", "e"], edge("b", "a"), edge("c", "b"),
                      edge("missing", "a"), edge("d", "a", "dynamic_call"),
                      edge("e", "a", confidence="unresolved"))
        self.assertEqual(graph.impacts("a", depth=0)[:2], ({}, False))
        one, truncated, _ = graph.impacts("a", depth=1, limit=1)
        self.assertEqual(set(one), {"b"})
        self.assertFalse(truncated)
        limited, truncated, gaps = graph.impacts("a", depth=2, limit=1)
        self.assertEqual(set(limited), {"b"})
        self.assertTrue(truncated)
        self.assertTrue(any("budget" in gap for gap in gaps))
        completed, truncated, _ = graph.impacts("a", depth=2, limit=2)
        self.assertEqual(set(completed), {"b", "c"})
        self.assertFalse(truncated)

    def test_repeated_edges_consume_work_even_without_new_nodes(self):
        graph = Graph(["a"])
        pulls = []

        def repeated(node, direction, gaps):
            while True:
                pulls.append(1)
                if len(pulls) > 300:
                    self.fail("unbounded edge enumeration")
                yield edge("a", "a")

        graph.neighbors = repeated
        hits, truncated, gaps = graph.impacts("a", limit=1)
        self.assertEqual(hits, {})
        self.assertTrue(truncated)
        self.assertIn("Impact edge scan budget reached", gaps)


if __name__ == "__main__":
    unittest.main()
