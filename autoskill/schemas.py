"""技能元数据、协议数据结构与 JSON Schema 入参校验。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

# 技能状态机：active -> quarantined（熔断） -> active（v2 热替换）；deprecated 预留
ACTIVE = "active"
QUARANTINED = "quarantined"
DEPRECATED = "deprecated"

RUNNER_PROTOCOL_EXIT_INTERNAL = 3  # runner 自身崩溃的退出码


@dataclass
class SkillMeta:
    """skill.json 的内存表示。"""

    name: str
    description: str
    tags: list[str]
    params_schema: dict
    version: int = 1
    status: str = ACTIVE
    fail_streak: int = 0
    history: list[dict] = field(default_factory=list)  # [{version, created_at, note}]
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "params_schema": self.params_schema,
            "version": self.version,
            "status": self.status,
            "fail_streak": self.fail_streak,
            "history": self.history,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "SkillMeta":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            tags=list(d.get("tags", [])),
            params_schema=d.get("params_schema") or {"type": "object", "properties": {}},
            version=int(d.get("version", 1)),
            status=d.get("status", ACTIVE),
            fail_streak=int(d.get("fail_streak", 0)),
            history=list(d.get("history", [])),
            created_at=float(d.get("created_at", time.time())),
            updated_at=float(d.get("updated_at", time.time())),
        )


def validate_params(params: Any, schema: dict) -> tuple[bool, str]:
    """按 JSON Schema 校验入参。返回 (ok, 错误信息)。"""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - 环境异常时降级放行，runner 侧兜底
        return True, ""
    try:
        jsonschema.validate(instance=params, schema=schema or {"type": "object"})
        return True, ""
    except jsonschema.ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path)
        return False, f"入参不符合 JSON Schema（{path or '根'}）: {e.message}"
    except Exception as e:  # schema 本身非法
        return False, f"params_schema 非法: {e}"


def parse_protocol_output(stdout: str) -> dict | None:
    """解析 runner 协议输出：取 stdout 最后一个非空行，尝试按 JSON 解析。"""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def values_equal(a: Any, b: Any) -> bool:
    """评测级精确相等：bool 严格区分、数值允许 int/float 等值、容器逐项比较。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= 1e-9
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a.keys()) == set(b.keys()) and all(values_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(values_equal(x, y) for x, y in zip(a, b))
    return a == b
