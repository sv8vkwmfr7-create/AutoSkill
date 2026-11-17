"""一键功能自检：工程级落地的持续验收入口（python -m autoskill selfcheck）。

六项检查对应验收体系的核心链路，全部通过即判定"功能可用"：
    1. 全链路生成（池外需求 -> 生成 -> 入库 -> 执行结果正确）
    2. 召回复用（同义改述 -> 走召回路径 -> 结果正确）
    3. 直接调用（按名调用 + 参数正确性）
    4. 重启重建（注册表对账后技能仍可用）
    5. Agent 调用（动态工具列表完成任务）
    6. 沙箱拒绝（敌意载荷被运行时层拦截，离线无 LLM）
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from .agent import Agent
from .service import AutoSkillService


def run_selfcheck(service: AutoSkillService | None = None, verbose: bool = True) -> dict:
    """在**独立临时技能池**上自检（不污染生产池），天然幂等可反复运行。"""
    tmp_base = None
    if service is None:
        import tempfile as _tf

        tmp_base = Path(_tf.mkdtemp(prefix="autoskill_selfcheck_"))
        from .config import Settings

        s = Settings.load()  # 加载 .env 中的真实 LLM 配置
        s.skills_dir = tmp_base / "skills"
        s.data_dir = tmp_base / "data"
        svc = AutoSkillService(s)
    else:
        svc = service
    checks: list[dict] = []
    skill_name_hint = "repeat_string_n_times"
    requirement = (
        "生成一个技能：把字符串 text 重复 repeat 次。参数：text（字符串）、repeat（整数）。"
        "run 直接返回重复后的字符串，不要包装成对象。示例：text='ab'、repeat=3 应得 'ababab'。"
        "技能名用 repeat_string_n_times。"
    )
    paraphrase = "帮我看看把 'ab' 这个字符串重复 3 次之后的结果是什么"

    def record(item: str, ok: bool, detail: str = "", elapsed: float | None = None) -> None:
        checks.append({"item": item, "ok": ok, "detail": detail[:200], "elapsed_s": elapsed})
        if verbose:
            flag = "PASS" if ok else "FAIL"
            tail = f" ({elapsed:.1f}s)" if elapsed is not None else ""
            print(f"[{flag}] {item}: {detail[:120]}{tail}", file=sys.stderr)

    # 1) 全链路生成 + 结果正确性
    t = time.monotonic()
    r = svc.ensure(requirement)
    ok1 = r.ok and r.path == "generated" and r.skill == skill_name_hint
    detail1 = f"skill={r.skill} path={r.path}" + (f" err={r.error[:120]}" if r.error else "")
    if r.ok and r.skill:
        v = svc.call(r.skill, {"text": "ab", "repeat": 3})
        ok1 = ok1 and v.ok and v.output == "ababab"
        detail1 += f" | call={v.ok} output={v.output!r}"
    record("1.全链路生成", ok1, detail1, time.monotonic() - t)
    skill_name = r.skill

    # 2) 召回复用
    t = time.monotonic()
    r2 = svc.ensure(paraphrase)
    ok2 = r2.ok and r2.path == "recall" and r2.output == "ababab"
    record("2.召回复用", ok2, f"path={r2.path} skill={r2.skill} output={r2.output!r}", time.monotonic() - t)

    # 3) 直接调用
    t = time.monotonic()
    if skill_name:
        r3 = svc.call(skill_name, {"text": "xy", "repeat": 2})
        ok3 = r3.ok and r3.output == "xyxy"
        record("3.直接调用", ok3, f"output={r3.output!r}", time.monotonic() - t)
    else:
        record("3.直接调用", False, "无可用技能（第 1 项失败）")

    # 4) 重启重建
    t = time.monotonic()
    rep = svc.rebuild()
    ok4 = rep["skills"] >= 1 and skill_name and svc.call(skill_name, {"text": "ab", "repeat": 3}).ok
    record("4.重启重建", ok4, json.dumps(rep, ensure_ascii=False), time.monotonic() - t)

    # 5) Agent 动态工具调用
    t = time.monotonic()
    try:
        reply = Agent(svc).chat("用技能把字符串 ab 重复 3 次，告诉我结果")
        ok5 = bool(reply.steps) and all(s.get("ok") for s in reply.steps)
        detail5 = f"rounds={reply.rounds} steps={len(reply.steps)} content={reply.content[:60]!r}"
        if not reply.steps:
            ok5 = False
            detail5 = f"未调用任何工具: {reply.content[:120]!r}"
    except Exception as e:  # noqa: BLE001
        ok5, detail5 = False, f"agent 异常: {e}"
    record("5.Agent 调用", ok5, detail5, time.monotonic() - t)

    # 6) 沙箱拒绝（离线：别名攻击在运行时被拒）
    t = time.monotonic()
    try:
        from autoskill.executor import LocalSubprocessExecutor

        d = Path(tempfile.mkdtemp()) / "hostile_chk"
        (d / "v1").mkdir(parents=True)
        (d / "skill.json").write_text(
            json.dumps({"name": "hostile_chk", "version": 1, "params_schema": {"type": "object"}}),
            encoding="utf-8",
        )
        (d / "v1" / "impl.py").write_text(
            "items = [open]\n\ndef run(params):\n    return items[0](params['path']).read()\n",
            encoding="utf-8",
        )
        r6 = LocalSubprocessExecutor().execute(d, {})
        ok6 = not r6.ok and ("安全" in r6.error or "not defined" in r6.error)
        record("6.沙箱拒绝", ok6, r6.error or "（意外放行）", time.monotonic() - t)
    except Exception as e:  # noqa: BLE001
        record("6.沙箱拒绝", False, f"检查异常: {e}", time.monotonic() - t)

    summary = {"ok": all(c["ok"] for c in checks), "checks": checks}
    if tmp_base is not None:
        import shutil

        shutil.rmtree(tmp_base, ignore_errors=True)  # 自检池用后即清，不污染生产数据
    if verbose:
        passed = sum(1 for c in checks if c["ok"])
        print(f"\n自检结果: {passed}/{len(checks)} 通过 -> {'功能可用' if summary['ok'] else '存在失败项'}", file=sys.stderr)
    return summary


def main() -> int:
    return run_selfcheck()["ok"]


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
