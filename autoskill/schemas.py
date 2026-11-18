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
    stable_version: int = 0  # 最近一个经连续成功验证的稳定版本（0 = 尚无；自动回退目标）
    promoted_successes: int = 0  # 当前生效版本晋升后的连续成功次数
    egress: dict | None = None  # HTTP 类技能的 egress 声明 {"allow_hosts": [...]}

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
            "stable_version": self.stable_version,
            "promoted_successes": self.promoted_successes,
            **({"egress": self.egress} if self.egress else {}),
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
            stable_version=int(d.get("stable_version", 0)),
            promoted_successes=int(d.get("promoted_successes", 0)),
            egress=d.get("egress"),
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


def match_expected(value: Any, expected: Any) -> bool:
    """期望匹配：精确值比较，或 _check 谓词（随机性输出的自测/评测通用）。

    _check 支持：type_is / length_eq / regex / contains_all（后两者为正则）。
    """
    import re as _re

    if isinstance(expected, dict) and "_check" in expected:
        checks = expected["_check"]
        if "type_is" in checks and type(value).__name__ != checks["type_is"]:
            return False
        if "length_eq" in checks and (not hasattr(value, "__len__") or len(value) != checks["length_eq"]):
            return False
        if "regex" in checks:
            if not isinstance(value, str) or not _re.search(checks["regex"], value):
                return False
        if "contains_all" in checks:
            if not isinstance(value, str):
                return False
            if not all(_re.search(p, value) for p in checks["contains_all"]):
                return False
        return True
    return values_equal(value, expected)
