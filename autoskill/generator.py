"""技能代码生成引擎：结构化输出 -> 语法/契约/静态安全/沙箱四层校验 -> 自修正循环。

失败分级（用于回喂 LLM 的修复提示与评测归因）：
    parse     JSON 解析失败 / 字段缺失非法 / 语法错误
    contract  缺少 run(params) 入口
    guard     import 白名单 / builtin 黑名单违规（安全层，修复也不应放行）
    sandbox   自测用例执行失败（含超时）
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .ast_guard import IMPORT_WHITELIST, check_code, check_contract
from .config import Settings
from .executor.base import Executor
from .llm import LLMClient, LLMFormatError
from .schemas import values_equal as _values_equal

logger = logging.getLogger("autoskill.generator")

GEN_SYSTEM = f"""你是 AutoSkill 系统的技能生成器，为 Agent 生产可执行的纯计算工具函数。
严格输出一个 JSON 对象（不要任何解释文字、不要 markdown 围栏），字段如下：
{{
  "name": "技能名，snake_case，[a-z][a-z0-9_]，3~50 字符，体现功能",
  "description": "一句话功能描述（将用于向量召回，不超过 50 字，写清动作与对象）",
  "tags": ["标签1", "标签2"],
  "params": {{"type": "object", "properties": {{"参数名": {{"type": "string|integer|number|boolean|array", "description": "参数说明"}}}}, "required": ["必填参数"]}},
  "impl_code": "完整的 impl.py 源码字符串（含 import）",
  "tests": [{{"input": {{}}, "expected": <与 input 精确对应的期望输出>}}]
}}
硬性约束：
1. impl_code 必须定义顶层入口 def run(params: dict)，返回值必须 JSON 可序列化
2. 返回值直接就是结果本身（数字/字符串/布尔/数组），仅当结果天然由多个字段组成时才返回 dict，且键名遵循需求指定的键名——不要为了包装而包装
3. 需求中指定的参数名必须原样作为 params 的键名，不得改名
4. 只允许 import 以下模块：{", ".join(sorted(IMPORT_WHITELIST))}
5. 禁止 eval/exec/compile/open/__import__/input/globals/locals/vars/breakpoint，禁止双下划线属性或名称，禁止相对导入
6. 禁止任何网络访问、文件读写、子进程、环境变量访问——这是纯计算技能
7. 能力边界（诚实拒绝）：若需求本质上需要网络请求、文件/系统访问、子进程、数据库等白名单无法真实实现的能力，必须输出 {{"reject": true, "reason": "该需求需要XX能力，超出纯计算技能边界"}}，严禁编造看似可用实则虚假的实现
8. tests 至少 3 组，覆盖典型与边界输入；expected 必须与 run 的实际返回结构完全一致（含键名与包装层级）
9. params 的 JSON Schema 必须与 impl_code 中实际读取的键完全一致"""


@dataclass
class GeneratedSkill:
    name: str
    description: str
    tags: list[str]
    params: dict
    impl_code: str
    tests: list[dict]


@dataclass
class GenOutcome:
    ok: bool
    skill: GeneratedSkill | None = None
    rounds: int = 0  # 实际使用的生成尝试次数（含首次；自修正 = rounds - 1）
    error: str = ""
    error_kind: str = ""  # parse | contract | guard | sandbox | unknown
    violations: list[str] = field(default_factory=list)
    terminal: bool = False  # 终态失败（如能力边界拒绝），不再自修正


class Generator:
    def __init__(self, llm: LLMClient, executor: Executor, settings: Settings):
        self.llm = llm
        self.executor = executor
        self.settings = settings

    # ---- 主流程 -----------------------------------------------------------
    def generate(self, requirement: str, target_name: str | None = None) -> GenOutcome:
        user_prompt = f"技能需求：\n{requirement.strip()}"
        if target_name:
            user_prompt += f"\n\n注意：该需求命中了一个已被熔断隔离的旧技能，本次是升级修复。技能名必须为：{target_name}"

        total_attempts = 1 + self.settings.max_repair_rounds
        feedback = ""
        outcome = GenOutcome(ok=False, error="未生成", error_kind="unknown")

        for attempt in range(1, total_attempts + 1):
            outcome = self._attempt(user_prompt + feedback, target_name, attempt)
            if outcome.ok:
                logger.info("生成成功 name=%s rounds=%d", outcome.skill.name, attempt)
                return outcome
            logger.warning("生成失败 attempt=%d kind=%s err=%s", attempt, outcome.error_kind, outcome.error[:200])
            if outcome.terminal:
                return outcome
            feedback = (
                f"\n\n【第 {attempt} 次尝试失败，阶段={outcome.error_kind}，请修复后重新输出完整 JSON（全量，不是 diff）】\n"
                f"错误详情：\n{outcome.error}\n"
                + (f"违规清单：\n" + "\n".join(f"- {v}" for v in outcome.violations) + "\n" if outcome.violations else "")
            )
        return outcome

    def _attempt(self, prompt: str, target_name: str | None, attempt: int) -> GenOutcome:
        # 1) LLM 生成
        try:
            raw = self.llm.chat_json(GEN_SYSTEM, prompt, max_tokens=8000)
        except LLMFormatError as e:
            return GenOutcome(ok=False, rounds=attempt, error=f"输出 JSON 无法解析: {e}", error_kind="parse", violations=[str(e)])
        except Exception as e:  # noqa: BLE001
            return GenOutcome(ok=False, rounds=attempt, error=f"LLM 调用失败: {e}", error_kind="unknown")

        # 1.5) 诚实拒绝：生成器判定需求超出能力边界（终态，不自修正）
        if raw.get("reject") is True:
            reason = str(raw.get("reason", "超出纯计算技能能力边界"))
            return GenOutcome(
                ok=False,
                rounds=attempt,
                error=f"生成器判定能力边界，拒绝生成: {reason}",
                error_kind="guard",
                violations=["能力边界: " + reason],
                terminal=True,
            )

        # 2) 结构校验
        skill, struct_err = self._parse_skill(raw, target_name)
        if struct_err:
            return GenOutcome(ok=False, rounds=attempt, error=struct_err, error_kind="parse")

        # 3) 语法检查
        try:
            compile(skill.impl_code, "impl.py", "exec")
        except SyntaxError as e:
            return GenOutcome(ok=False, rounds=attempt, error=f"impl_code 语法错误: 第 {e.lineno} 行 {e.msg}", error_kind="parse")

        # 4) 契约检查
        contract = check_contract(skill.impl_code)
        if not contract.ok:
            return GenOutcome(ok=False, rounds=attempt, error="技能契约不满足: " + "; ".join(contract.violations), error_kind="contract", violations=contract.violations)

        # 5) 静态安全检查（AST 白名单）
        guard = check_code(skill.impl_code)
        if not guard.ok:
            return GenOutcome(ok=False, rounds=attempt, error="违反静态安全白名单（能力边界，禁止放行）", error_kind="guard", violations=guard.violations)

        # 6) 沙箱跑自测用例
        sandbox_err = self._run_tests(skill)
        if sandbox_err:
            return GenOutcome(ok=False, rounds=attempt, error=sandbox_err, error_kind="sandbox")

        return GenOutcome(ok=True, skill=skill, rounds=attempt)

    # ---- 辅助 -------------------------------------------------------------
    def _parse_skill(self, raw: dict, target_name: str | None) -> tuple[GeneratedSkill | None, str]:
        name = target_name or raw.get("name")
        if not isinstance(name, str) or not name:
            return None, "缺少 name 或非字符串"
        name = name.strip().lower()
        if target_name:
            name = target_name
        if not all(c.isalnum() or c == "_" for c in name) or not name[0].isalpha() or name[0].isupper() or len(name) < 3 or len(name) > 50:
            return None, f"name 不符合规范: {name!r}（snake_case，3~50 字符）"
        description = raw.get("description")
        if not isinstance(description, str) or len(description.strip()) < 5:
            return None, "缺少有效 description（≥5 字符，将用于向量召回）"
        params = raw.get("params")
        if not isinstance(params, dict):
            return None, "缺少 params（JSON Schema 对象）"
        params.setdefault("type", "object")
        impl_code = raw.get("impl_code")
        if not isinstance(impl_code, str) or len(impl_code.strip()) < 20:
            return None, "缺少 impl_code 或过短"
        tests = raw.get("tests")
        if not isinstance(tests, list) or len(tests) < 3:
            return None, f"tests 必须是 ≥3 组用例的数组（当前: {len(tests) if isinstance(tests, list) else '非数组'}）"
        clean_tests = []
        for i, t in enumerate(tests):
            if not isinstance(t, dict) or not isinstance(t.get("input"), dict) or "expected" not in t:
                return None, f"tests[{i}] 必须包含 input(object) 与 expected"
            clean_tests.append({"input": t["input"], "expected": t["expected"]})
        tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
        tags = [str(t) for t in tags][:8]
        skill = GeneratedSkill(name=name, description=description.strip()[:200], tags=tags, params=params, impl_code=impl_code, tests=clean_tests)
        return skill, ""

    def _run_tests(self, skill: GeneratedSkill) -> str:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="autoskill_staging_", dir=str(self.settings.data_dir)))
        try:
            skill_dir = staging / skill.name
            (skill_dir / "v1").mkdir(parents=True)
            (skill_dir / "skill.json").write_text(
                json.dumps({"name": skill.name, "version": 1, "params_schema": skill.params}, ensure_ascii=False),
                encoding="utf-8",
            )
            (skill_dir / "v1" / "impl.py").write_text(skill.impl_code, encoding="utf-8")
            (skill_dir / "v1" / "tests.json").write_text(
                json.dumps(skill.tests, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            problems = []
            for i, case in enumerate(skill.tests):
                result = self.executor.execute(skill_dir, case["input"], timeout_s=self.settings.exec_timeout_s)
                if result.timed_out:
                    problems.append(f"用例[{i}] 执行超时（>{self.settings.exec_timeout_s}s）")
                elif result.exit_code not in (0, None):
                    problems.append(f"用例[{i}] runner 崩溃 code={result.exit_code}: {result.stderr[-200:]}")
                elif not result.ok:
                    problems.append(f"用例[{i}] 运行失败: {result.error[:300]}")
                elif not _values_equal(result.data, case["expected"]):
                    problems.append(f"用例[{i}] 结果不符: got={json.dumps(result.data, ensure_ascii=True, default=str)[:200]} want={json.dumps(case['expected'], ensure_ascii=True, default=str)[:200]}")
            if problems:
                return "沙箱自测未通过（共 %d 组用例）:\n" % len(skill.tests) + "\n".join(problems)
            return ""
        finally:
            # 清理 staging（impl 代码只在子进程中执行过，主进程不 import 它）
            import shutil

            shutil.rmtree(staging, ignore_errors=True)
