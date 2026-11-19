"""轻量 Prometheus 指标（零依赖，文本暴露格式）。

计数器 + 观测值（count/sum/avg）。线程安全；/metrics 端点直接输出 render()。
"""

from __future__ import annotations

import threading


def _safe_name(name: str) -> str:
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name).strip("_").lower()


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[str, float] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            key = _safe_name(name)
            self._values[key] = self._values.get(key, 0.0) + value

    def observe(self, name: str, value: float) -> None:
        """观测值：记录 count/sum/avg 三个序列。"""
        with self._lock:
            base = _safe_name(name)
            self._values[base + "_count"] = self._values.get(base + "_count", 0.0) + 1
            self._values[base + "_sum"] = self._values.get(base + "_sum", 0.0) + value

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return dict(self._values)

    def render(self) -> str:
        """Prometheus 文本暴露格式（含 HELP/TYPE 注释）。"""
        snap = self.snapshot()
        lines: list[str] = []
        emitted: set[str] = set()
        for key in sorted(snap):
            family = key
            for suffix in ("_count", "_sum", "_avg"):
                if key.endswith(suffix):
                    family = key[: -len(suffix)]
                    break
            if family in emitted:
                continue
            emitted.add(family)
            lines.append(f"# TYPE {family} summary")
            if family + "_count" in snap:
                lines.append(f"{family}_count {snap[family + '_count']:.0f}")
                lines.append(f"{family}_sum {snap.get(family + '_sum', 0.0):.3f}")
                cnt = snap.get(family + "_count", 0.0)
                if cnt:
                    lines.append(f"{family}_avg {snap.get(family + '_sum', 0.0) / cnt:.3f}")
                else:
                    lines.append(f"{family}_avg 0")
            else:
                lines.append(f"{family} {snap[key]:.0f}")
        return "\n".join(lines) + "\n"
