from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    kind: str
    box: tuple[int, int, int, int]  # x, y, width, height
    color: str = "#000000"
    background: str = ""
    text: str = ""
    confidence: float = 1.0
    children: list["Node"] = field(default_factory=list)
    image_data: str = ""
    vector_data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        result = {
            "kind": self.kind,
            "box": list(self.box),
            "color": self.color,
            "background": self.background,
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "children": [child.to_dict() for child in self.children],
        }
        if self.vector_data:
            result["vector_data"] = self.vector_data
        return result


def flatten(node: Node):
    yield node
    for child in node.children:
        yield from flatten(child)
