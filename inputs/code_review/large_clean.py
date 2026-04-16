"""A large, clean module.

LOC exceeds the guard's 200-line threshold, so the cheap guard predicts
request_changes. Ruff + bandit both report nothing, so the real verdict is
approve. Second rollback-measurement case.
"""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class Node:
    node_id: str
    label: str
    weight: float = 1.0


@dataclass
class Edge:
    src: str
    dst: str
    weight: float = 1.0


class Graph:
    def __init__(self) -> None:
        self._nodes: Dict[str, Node] = {}
        self._edges: List[Edge] = []

    def add_node(self, node: Node) -> None:
        self._nodes[node.node_id] = node

    def add_edge(self, edge: Edge) -> None:
        if edge.src not in self._nodes:
            raise KeyError(edge.src)
        if edge.dst not in self._nodes:
            raise KeyError(edge.dst)
        self._edges.append(edge)

    def neighbors(self, node_id: str) -> List[str]:
        return [e.dst for e in self._edges if e.src == node_id]

    def node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def nodes(self) -> Iterable[Node]:
        return self._nodes.values()

    def edges(self) -> Iterable[Edge]:
        return iter(self._edges)

    def degree(self, node_id: str) -> int:
        return sum(1 for e in self._edges if e.src == node_id or e.dst == node_id)

    def total_weight(self) -> float:
        return sum(e.weight for e in self._edges)

    def has_cycle(self) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def dfs(u: str) -> bool:
            if u in visiting:
                return True
            if u in visited:
                return False
            visiting.add(u)
            for v in self.neighbors(u):
                if dfs(v):
                    return True
            visiting.discard(u)
            visited.add(u)
            return False

        for node_id in list(self._nodes.keys()):
            if dfs(node_id):
                return True
        return False


def topological_sort(graph: Graph) -> List[str]:
    indegree: Dict[str, int] = {n.node_id: 0 for n in graph.nodes()}
    for edge in graph.edges():
        indegree[edge.dst] = indegree.get(edge.dst, 0) + 1

    queue = [node_id for node_id, d in indegree.items() if d == 0]
    out: List[str] = []
    while queue:
        current = queue.pop(0)
        out.append(current)
        for neighbor in graph.neighbors(current):
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                queue.append(neighbor)
    return out


def shortest_path_lengths(
    graph: Graph, source: str
) -> Dict[str, float]:
    distances: Dict[str, float] = {n.node_id: float("inf") for n in graph.nodes()}
    distances[source] = 0.0
    visited: set[str] = set()

    while len(visited) < len(distances):
        current: Optional[str] = None
        best = float("inf")
        for node_id, dist in distances.items():
            if node_id in visited:
                continue
            if dist < best:
                best = dist
                current = node_id
        if current is None:
            break
        visited.add(current)
        for neighbor in graph.neighbors(current):
            weight = next(
                (e.weight for e in graph.edges() if e.src == current and e.dst == neighbor),
                1.0,
            )
            candidate = distances[current] + weight
            if candidate < distances[neighbor]:
                distances[neighbor] = candidate

    return distances


def build_sample_graph() -> Graph:
    graph = Graph()
    for i in range(20):
        graph.add_node(Node(node_id=f"n{i}", label=f"node-{i}"))
    for i in range(19):
        graph.add_edge(Edge(src=f"n{i}", dst=f"n{i + 1}", weight=float(i + 1)))
    return graph


def summarize_graph(graph: Graph) -> Tuple[int, int, float]:
    return (
        sum(1 for _ in graph.nodes()),
        sum(1 for _ in graph.edges()),
        graph.total_weight(),
    )


def find_longest_chain(graph: Graph) -> int:
    longest = 0
    for root in graph.nodes():
        stack: List[Tuple[str, int]] = [(root.node_id, 0)]
        while stack:
            node_id, depth = stack.pop()
            longest = max(longest, depth)
            for neighbor in graph.neighbors(node_id):
                stack.append((neighbor, depth + 1))
    return longest


def pretty_print(graph: Graph) -> str:
    lines = ["Graph:"]
    for node in graph.nodes():
        lines.append(f"  {node.node_id} ({node.label})")
    for edge in graph.edges():
        lines.append(f"  {edge.src} --[{edge.weight}]-> {edge.dst}")
    return "\n".join(lines)


def clone(graph: Graph) -> Graph:
    copy = Graph()
    for node in graph.nodes():
        copy.add_node(Node(node_id=node.node_id, label=node.label, weight=node.weight))
    for edge in graph.edges():
        copy.add_edge(Edge(src=edge.src, dst=edge.dst, weight=edge.weight))
    return copy


def merge(a: Graph, b: Graph) -> Graph:
    out = clone(a)
    for node in b.nodes():
        if out.node(node.node_id) is None:
            out.add_node(Node(node_id=node.node_id, label=node.label, weight=node.weight))
    for edge in b.edges():
        out.add_edge(Edge(src=edge.src, dst=edge.dst, weight=edge.weight))
    return out


def filter_nodes(graph: Graph, predicate) -> Graph:
    out = Graph()
    for node in graph.nodes():
        if predicate(node):
            out.add_node(Node(node_id=node.node_id, label=node.label, weight=node.weight))
    for edge in graph.edges():
        if out.node(edge.src) is not None and out.node(edge.dst) is not None:
            out.add_edge(Edge(src=edge.src, dst=edge.dst, weight=edge.weight))
    return out


def heavy_nodes(graph: Graph, threshold: float) -> List[Node]:
    return [n for n in graph.nodes() if n.weight >= threshold]


def edge_histogram(graph: Graph) -> Dict[int, int]:
    buckets: Dict[int, int] = {}
    for edge in graph.edges():
        key = int(edge.weight)
        buckets[key] = buckets.get(key, 0) + 1
    return buckets


if __name__ == "__main__":
    g = build_sample_graph()
    print(pretty_print(g))
    print("topo:", topological_sort(g))
    print("summary:", summarize_graph(g))
    print("longest chain:", find_longest_chain(g))
    print("has_cycle:", g.has_cycle())
    print("distances:", shortest_path_lengths(g, "n0"))
    print("heavy:", [n.node_id for n in heavy_nodes(g, 1.0)])
    print("histogram:", edge_histogram(g))
    print("cloned nodes:", sum(1 for _ in clone(g).nodes()))
