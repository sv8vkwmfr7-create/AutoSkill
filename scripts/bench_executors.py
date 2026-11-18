"""执行器对比基准：同一组需求在 local / docker 两种沙箱后端下复跑，产出对比报告。

用法（需宿主已安装 Docker 才能跑 docker 侧）：
    python scripts/bench_executors.py                # 双后端对比，输出 eval/bench_executors.md
    python scripts/bench_executors.py --local-only   # 仅 local 侧（无 Docker 环境自检）

说明：两个后端各自使用独立临时技能池；相同需求、相同模型配置，测量
生成端到端耗时 / 执行耗时 / 成功率，并校验执行器隔离层（越权访问必须被拒）。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from autoskill.config import Settings  # noqa: E402
from autoskill.service import AutoSkillService  # noqa: E402

# 固定的 6 条基准需求（人工可验证的确定性输出）
BENCH_ITEMS = [
    {"id": "N01", "q": "计算文本 hello 的 SHA-256 哈希，直接返回 64 位小写十六进制字符串",
     "expect": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"},
    {"id": "N02", "q": "把字符串 hello 反转，直接返回反转后的字符串", "expect": "olleh"},
    {"id": "N03", "q": "计算 12 和 18 的最大公约数，直接返回整数", "expect": 6},
    {"id": "N04", "q": "把驼峰命名 HTTPServer 转换为 snake_case 小写下划线形式，直接返回字符串", "expect": "http_server"},
    {"id": "N05", "q": "用 Luhn 算法校验卡号 79927398713 是否有效，直接返回布尔值", "expect": True},
    {"id": "N06", "q": "把字符串 hello 做 Base64 编码，直接返回编码结果字符串", "expect": "aGVsbG8="},
]


def make_backend(tmp_base: Path, executor: str) -> AutoSkillService:
    s = Settings.load()
    s.skills_dir = tmp_base / f"skills_{executor}"
    s.data_dir = tmp_base / f"data_{executor}"
    s.vector_store = "local"
    s.embedding_backend = "local"
    s.executor = executor
    return AutoSkillService(s)


def run_leg(executor: str) -> dict:
    tmp_base = Path(tempfile.mkdtemp(prefix=f"autoskill_bench_{executor}_"))
    rows = []
    try:
        svc = make_backend(tmp_base, executor)
        for item in BENCH_ITEMS:
            t = time.monotonic()
            r = svc.ensure(item["q"])
            total = time.monotonic() - t
            ok = r.ok and r.output == item["expect"]
            rows.append({
                "id": item["id"],
                "ok": bool(ok),
                "path": r.path,
                "skill": r.skill,
                "exec_s": round(r.elapsed_s.get("exec_s", 0.0), 3),
                "total_s": round(total, 2),
                "error": (r.error or "")[:120],
            })
        # 隔离层对抗用例：越权读取必须被拒
        from autoskill.executor import make_executor as _mk

        d = tmp_base / "hostile"
        (d / "v1").mkdir(parents=True)
        (d / "skill.json").write_text(
            json.dumps({"name": "hostile", "version": 1, "params_schema": {"type": "object"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (d / "v1" / "impl.py").write_text(
            "items = [open]\n\ndef run(params):\n    return items[0](params['path']).read()\n",
            encoding="utf-8",
        )
        ex = _mk(Settings.load()) if executor == "local" else None
        if ex is None:
            from autoskill.executor import DockerExecutor

            ex = DockerExecutor()
        hostile_res = ex.execute(d, {})
        hostile_denied = not hostile_res.ok
        rows.append({"id": "SEC01", "ok": hostile_denied, "path": "security",
                     "skill": "-", "exec_s": 0, "total_s": 0,
                     "error": "" if hostile_denied else "越权读取未被拦截"})
        return {"executor": executor, "rows": rows}
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)


def render(results: list[dict]) -> str:
    lines = ["# 执行器对比基准（local vs docker）", ""]
    for leg in results:
        passed = sum(1 for r in leg["rows"] if r["ok"])
        lines += [
            f"## 后端：{leg['executor']}（{passed}/{len(leg['rows'])} 通过）", "",
            "| id | 判定 | 路径 | 技能 | 执行耗时s | 端到端s | 错误 |",
            "|----|------|------|------|-----------|---------|------|",
        ]
        for r in leg["rows"]:
            lines.append(
                f"| {r['id']} | {'✅' if r['ok'] else '❌'} | {r['path']} | {r['skill'] or '-'} "
                f"| {r['exec_s']} | {r['total_s']} | {r['error']} |"
            )
        lines.append("")
    lines += [
        "## 指标说明",
        "",
        "- `exec_s`：沙箱内技能执行耗时（子进程/容器生命周期）",
        "- `total_s`：含 LLM 生成/裁决的端到端耗时",
        "- `SEC01`：别名攻击越权读取，任何后端都必须显式拒绝",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    executors = ["local", "docker"]
    if "--local-only" in sys.argv:
        executors = ["local"]
    results = []
    for executor in executors:
        print(f"=== 运行 {executor} 后端 ===", file=sys.stderr)
        try:
            results.append(run_leg(executor))
        except Exception as e:  # noqa: BLE001
            print(f"[{executor}] 后端不可用: {type(e).__name__}: {e}", file=sys.stderr)
            if executor == "docker":
                print("提示：docker 侧需要宿主安装并启动 Docker（见 docs/DEPLOY.md）", file=sys.stderr)
                results.append({"executor": "docker", "rows": [
                    {"id": "N01", "ok": False, "path": "unavailable", "skill": "-",
                     "exec_s": 0, "total_s": 0, "error": f"docker 不可用: {e}"}
                ]})
    out = PROJECT_ROOT / "eval" / "bench_executors.md"
    out.write_text(render(results), encoding="utf-8")
    print(f"对比报告已写入 {out}", file=sys.stderr)
    all_ok = all(r["ok"] for leg in results for r in leg["rows"] if r["id"] == "SEC01")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
