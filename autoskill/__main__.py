"""命令行入口：python -m autoskill <cmd>

子命令（验收演示命令，README 与之一致）：
    serve                启动 FastAPI 服务
    ensure "<需求>"      全链路：召回/生成 -> 校验 -> 入库 -> 调用
    call <name> --params '{"k":v}'    直接调用已注册技能
    skills               查看技能注册表
    rebuild              手动触发重启重建（正常由 serve 自动执行）
    agent "<消息>"       单轮 Agent 对话（含工具调用过程展示）
    eval                 跑批量评测并输出报告
"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autoskill", description="AutoSkill 自进化技能池")
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动 FastAPI 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    p_ensure = sub.add_parser("ensure", help="全链路：召回/生成 -> 校验 -> 入库 -> 调用")
    p_ensure.add_argument("requirement")

    p_call = sub.add_parser("call", help="直接调用技能")
    p_call.add_argument("name")
    p_call.add_argument("--params", default="{}", help="JSON 对象字符串")

    sub.add_parser("skills", help="查看技能注册表")
    sub.add_parser("rebuild", help="重建注册表与向量库对账")

    p_agent = sub.add_parser("agent", help="单轮 Agent 对话")
    p_agent.add_argument("message")

    p_eval = sub.add_parser("eval", help="批量评测")
    p_eval.add_argument("--evalset", default=None, help="评测集路径（默认 eval/evalset.json）")
    p_eval.add_argument("--skip-seed", action="store_true", help="跳过种子技能生成（池已就绪时）")
    p_eval.add_argument("--out", default=None, help="报告输出目录（默认 eval/）")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
        return 0

    from .service import AutoSkillService

    svc = AutoSkillService()

    if args.cmd == "ensure":
        result = svc.ensure(args.requirement)
        _print(result.as_dict())
        return 0 if result.ok else 1

    if args.cmd == "call":
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError as e:
            print(f"--params 不是合法 JSON: {e}", file=sys.stderr)
            return 2
        result = svc.call(args.name, params)
        _print(result.as_dict())
        return 0 if result.ok else 1

    if args.cmd == "skills":
        _print(svc.list_skills())
        return 0

    if args.cmd == "rebuild":
        _print(svc.rebuild())
        return 0

    if args.cmd == "agent":
        from .agent import Agent

        reply = Agent(svc).chat(args.message)
        _print(reply.as_dict())
        return 0

    if args.cmd == "eval":
        from .evaluation import run_eval

        report = run_eval(evalset_path=args.evalset, out_dir=args.out, skip_seed=args.skip_seed, service=svc)
        metrics = report["metrics"]
        print(
            f"\n评测完成: 生成成功率={metrics['generation_success_rate']:.0%} "
            f"路由正确率={metrics['routing_accuracy']:.0%} "
            f"静默失败={metrics['silent_failures']} 白名单拦截={metrics['guard_block_rate']:.0%}",
            file=sys.stderr,
        )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
