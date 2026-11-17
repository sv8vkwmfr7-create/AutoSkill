# AutoSkill 现场演示命令清单（验收 G1 对应）

> 每条命令对应验收项，附实测耗时与预期输出。全部命令在项目根目录执行；
> 首次演示前确保 `.env` 已配置（见 README 快速开始）。
> 实测环境：Windows 11 / Python 3.14.7 / deepseek-flash / Chroma / local executor（dev-only）。

## 准备：查看技能池现状

```bash
python -m autoskill skills        # 列出注册表：名称/版本/状态/熔断计数
```

评测预置池（`python -m autoskill eval` 产物）含 20 个技能：10 个种子 + 10 个评测生成。

---

## G1-1 全链路自动生成（门槛：≤60s、无人工干预、结果正确）

```bash
python -m autoskill ensure "生成一个技能：计算半径为 r 的圆的面积，pi 使用 math.pi，run 直接返回四舍五入保留 2 位小数的数字。示例：r=2 应得 12.57"
```

实测：**7.1s**，`{"ok": true, "path": "generated", "skill": "circle_area", ...}`。
日志可见完整链路：LLM 生成 → AST 校验 → 沙箱自测 → 注册 v1 → Chroma 入库 → 参数提取 → 执行。

验证结果正确性：

```bash
python -m autoskill call circle_area --params '{"r": 7}'
# 预期：{"ok": true, "output": 153.94}   （π×49 = 153.93804）
```

## G1-2 召回复用（门槛：≤10s、走召回路径、不触发生成）

```bash
python -m autoskill ensure "帮我算一下 2026-11-16 到 2026-11-20 期间有几个工作日（周一到周五，头尾都算），直接告诉我天数"
```

实测：**约 0.8~1.6s**，`{"ok": true, "path": "recall", "skill": "workday_count", "output": 5}`。
（评测 A 组 10 条同义改述全部召回命中，见 `eval/report.md`。）

## G1-3 熔断自修复（门槛：全程无人工干预、状态机正确）

```bash
# 1) 注入 bug（演示用，模拟线上事故）
python -c "from pathlib import Path; Path('skills/int_to_roman/v1/impl.py').write_text('def run(params):\\n    raise ValueError(\"injected bug\")\\n', encoding='utf-8')"

# 2) 连续 3 次必然失败 -> 熔断
python -m autoskill call int_to_roman --params '{"n": 1994}'   # ×3，均 ok=false
python -m autoskill skills    # int_to_roman v1 quarantined fail_streak=3

# 3) 第 4 次调用被显式拒绝
python -m autoskill call int_to_roman --params '{"n": 1994}'
# 预期 error: 技能已熔断隔离（连续失败 3 次），等待自修复升级

# 4) 同类需求 -> 自动检测隔离技能 -> 同名生成 v2 -> 热替换 active
python -m autoskill ensure "把 2026 转换成罗马数字"
# 实测：约 10~15s，path=generated，skill=int_to_roman，detail.repair_target=int_to_roman，version=2

# 5) v2 调用验证（注意：v2 可能使用新参数名，以 `python -m autoskill skills` 输出的 params_schema 为准；
#    用旧参数名调用会被 schema 预校验显式拒绝——这是预期行为，不是静默失败）
python -m autoskill call int_to_roman --params '{"number": 1994}'
# 预期：{"ok": true, "output": "MCMXCIV"}
```
```

## G1-4 Agent 调用（门槛：动态工具列表、新技能即时可用）

```bash
# 第 1 轮：调用已有技能
python -m autoskill agent "把 IP 地址 192.168.1.1 转换成整数"
# 预期：工具步骤 skill=ipv4 相关技能 ok=true，回答含 3232235777

# 生成一个新技能（入池）
python -m autoskill ensure "生成一个技能：计算半径为 r 的圆的面积，pi 使用 math.pi，run 直接返回四舍五入保留 2 位小数的数字。示例：r=2 应得 12.57"

# 第 2 轮（不重启）：新技能立即可用
python -m autoskill agent "半径 7 的圆面积是多少？"
# 预期：工具步骤 circle_area ok=true，回答含 153.94
```

## G1-5 重启恢复（门槛：注册表重建、状态保持、逐技能可用）

```bash
python -m autoskill serve --port 8000        # 终端 A
curl http://127.0.0.1:8000/health            # skills=N, rebuild{...}
curl -X POST http://127.0.0.1:8000/skills/int_to_roman/call -H "content-type: application/json" -d "{\"params\": {\"n\": 1994}}"
# Ctrl+C 停止终端 A，重新启动：
python -m autoskill serve --port 8000        # 再起服务
curl http://127.0.0.1:8000/health            # rebuild: skills=N, indexed=N, removed_orphans=0
curl http://127.0.0.1:8000/skills            # 版本/状态与重启前一致
```

---

## G2 批量评测（一键复现）

```bash
python -m autoskill eval        # 35 条冻结评测集，约 10~15 分钟
# 产物：eval/report.md + eval/report.json
```

最近一次实测（见 `eval/report.md`）：

| 指标 | 实测 | 门槛 |
|------|------|------|
| 池外生成校验成功率 | 100%（10/10） | ≥85% |
| 池内路由正确率 | 100%（10/10） | ≥90% |
| 静默失败数 | 0 | =0 |
| import 白名单拦截率 | 100%（5/5） | 100% |
| 平均耗时 | 召回 ~1s / 生成 ~9s | G1 门槛内 |
