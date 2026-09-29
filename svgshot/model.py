from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Node:
    kind: str
    box: tuple[int, int, int, int]  # x, y, width, height
    color: str = "#000000"
    text: str = ""
    confidence: float = 1.0
    children: list["Node"] = field(default_factory=list)
    image_data: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "box": list(self.box),
            "color": self.color,
            "text": self.text,
            "confidence": round(self.confidence, 3),
            "children": [child.to_dict() for child in self.children],
        }


def flatten(node: Node):
    yield node
    for child in node.children:
        yield from flatten(child)
