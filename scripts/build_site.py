"""构建 GitHub Pages 静态作品集 → site/ 目录（CI 自动部署到 gh-pages）。

产物:
    site/index.html    作品集首页（四指标卡片 / 演示 GIF / 链接）
    site/report.html   最新评测报告（Markdown 渲染）
    site/assets/demo.gif

用法: python scripts/build_site.py   （需 markdown 包：pip install markdown）
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import markdown
except ImportError:
    print("需要 markdown 包：pip install markdown", file=sys.stderr)
    sys.exit(1)

STYLE = """
:root { --bg:#0f1420; --card:#171e2e; --fg:#dbe2f0; --dim:#8b97ad; --accent:#4f8cff; --ok:#3fb27f; --bad:#e2606c; }
body { background:var(--bg); color:var(--fg); font-family:"Microsoft YaHei",system-ui,sans-serif; margin:0; padding:32px; max-width:960px; margin-inline:auto; }
a { color:var(--accent); text-decoration:none; }
h1,h2,h3 { color:var(--fg); }
.badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:12px; margin-right:6px; }
.card { background:var(--card); border-radius:12px; padding:20px; margin:16px 0; }
.metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:14px; }
.metric { background:var(--card); border-radius:12px; padding:16px; text-align:center; }
.metric .v { font-size:30px; font-weight:700; }
.metric .l { color:var(--dim); font-size:12px; margin-top:4px; }
.pass { color:var(--ok); } .fail { color:var(--bad); }
img { max-width:100%; border-radius:10px; }
pre, code { background:#0c111c; border-radius:6px; }
pre { padding:12px; overflow:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #232c40; }
.footer { color:var(--dim); font-size:12px; margin-top:28px; text-align:center; }
"""

HEADER = """<div style="display:flex;justify-content:space-between;align-items:center">
<div><b>AutoSkill</b> · 面向 Agent 的自进化技能生成与热加载系统</div>
<div><a href="https://github.com/sv8vkwmfr7-create/AutoSkill">GitHub</a> · <a href="index.html">首页</a> · <a href="report.html">评测报告</a></div>
</div>
<hr style="border-color:#232c40">"""


def metric_card(label: str, value: str, sub: str, ok: bool) -> str:
    color = "pass" if ok else "fail"
    return (f'<div class="metric"><div class="v {color}">{value}</div>'
            f'<div class="l">{label}<br>{sub}</div></div>')


def build() -> int:
    root = PROJECT_ROOT
    report_md = root / "eval" / "report.md"
    report_json = root / "eval" / "report.json"
    if not report_json.exists():
        print("缺少 eval/report.json —— 先运行 python -m autoskill eval", file=sys.stderr)
        return 1
    data = json.loads(report_json.read_text(encoding="utf-8"))
    m = data["metrics"]
    meta = data["meta"]

    gate = m["gate"]
    site = root / "site"
    site.mkdir(exist_ok=True)
    (site / "assets").mkdir(exist_ok=True)

    gif_src = root / "docs" / "assets" / "demo.gif"
    if gif_src.exists():
        shutil.copyfile(gif_src, site / "assets" / "demo.gif")

    cards = "".join(
        [
            metric_card("池外生成成功率", f"{m['generation_success_rate']:.0%}",
                        f"{sum(1 for x in data['out_pool'] if x['ok'])}/{m['generation_total']} · 门槛 85%",
                        gate["generation_ok(>=85%)"]),
            metric_card("池内路由正确率", f"{m['routing_accuracy']:.0%}",
                        f"{sum(1 for x in data['in_pool'] if x['ok'])}/{m['routing_total']} · 门槛 90%（含英文用例）",
                        gate["routing_ok(>=90%)"]),
            metric_card("静默失败", str(m["silent_failures"]), "门槛 0（失败必须显式）",
                        gate["silent_ok(=0)"]),
            metric_card("注入拦截率", f"{m['guard_block_rate']:.0%}",
                        f"{sum(1 for x in data['injection'] if x['ok'])}/{m['injection_total']} · 门槛 100%",
                        gate["guard_ok(=100%)"]),
        ]
    )

    index = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>AutoSkill — 自进化技能系统</title>
<style>{STYLE}</style></head><body>
{HEADER}
<h1>AutoSkill</h1>
<p>普通 Agent 调用工具；AutoSkill 的 Agent <b>生产工具</b>——需求未覆盖时 LLM 自动生成工具函数，
经五层沙箱纵深校验后入库热加载，Agent 无需重启即可调用；技能故障自动熔断、影子验证修复、自动回滚。</p>
<p><img src="assets/demo.gif" alt="熔断自修复演示"></p>
<div class="card"><h2>最新评测（45 条冻结评测集 · {meta['date'][:10]} · {meta['model']}）</h2>
<div class="metrics">{cards}</div>
<p style="font-size:12px;color:var(--dim)">完整逐条明细见 <a href="report.html">评测报告</a>；判分为人工预写冻结期望值（与 LLM 自生成测试隔离）。</p></div>
<div class="card"><h2>核心能力</h2>
<ul>
<li>全链路自动生成：召回 → LLM 终审 → 生成（四层校验 + ≤3 轮自修正）→ 入库热加载 → 调用</li>
<li>治理闭环：熔断隔离 → 影子验证修复 → 自动回滚 → 自动回退到最近稳定版本</li>
<li>五层沙箱纵深：AST 白名单 / 受限内建命名空间 / PEP 578 审计钩子 / 内存看门狗 / Docker 可切换</li>
<li>egress 域名白名单：HTTP 类技能四点校验（操作员拥有出站策略）</li>
<li>跨语言召回：混合部署（chat 与 embedding 独立配置），英文改述召回中文技能</li>
</ul></div>
<div class="footer">本站由 CI 自动构建（eval/report.json → HTML）·
<a href="https://github.com/sv8vkwmfr7-create/AutoSkill">源码</a> · MIT License</div>
</body></html>"""
    (site / "index.html").write_text(index, encoding="utf-8")

    if report_md.exists():
        body = markdown.markdown(report_md.read_text(encoding="utf-8"), extensions=["tables"])
        report_html = (f'<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
                       f"<title>AutoSkill 评测报告</title><style>{STYLE}</style></head><body>{HEADER}"
                       f"<h1>评测报告（自动生成）</h1>{body}"
                       '<div class="footer"><a href="index.html">返回首页</a></div></body></html>')
        (site / "report.html").write_text(report_html, encoding="utf-8")

    print(f"site 构建完成: {site}（index.html / report.html / assets）")
    return 0


if __name__ == "__main__":
    sys.exit(build())
