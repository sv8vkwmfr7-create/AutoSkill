"""生成 README 演示 GIF：以真实运行输出为脚本，渲染终端风格帧并合成动画。

用法: python scripts/make_demo_gif.py   （需 Pillow；输出 docs/assets/demo.gif）
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("需要 Pillow：uv pip install pillow", file=sys.stderr)
    sys.exit(1)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"  # 微软雅黑（覆盖中英文）
FONT_SIZE = 15
WIDTH, HEIGHT = 880, 420
BG = (13, 17, 23)
FG = (200, 210, 225)
PROMPT = (97, 197, 255)
OK = (63, 178, 127)
BAD = (226, 96, 108)
DIM = (110, 120, 140)


def load_font():
    for candidate in (FONT_PATH, "C:/Windows/Fonts/msyh.ttf", "C:/Windows/Fonts/simhei.ttf"):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, FONT_SIZE)
    return ImageFont.load_default()


def frame(lines: list[tuple[str, tuple]]) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    font = load_font()
    draw.rectangle([0, 0, WIDTH, 30], fill=(30, 37, 52))
    draw.text((14, 6), "AutoSkill — 熔断自修复演示（真实运行输出）", font=font, fill=FG)
    y = 46
    for text, color in lines:
        draw.text((16, y), text, font=font, fill=color)
        y += 24
    return img


def main() -> int:
    out_dir = PROJECT_ROOT / "docs" / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[tuple[list[tuple[str, tuple]], int]] = []

    frames.append(([
        ('$ autoskill ensure "生成一个技能：把字符串 ab 重复 3 次"', PROMPT),
        ("", FG),
        ("  生成 -> 静态校验 -> 沙箱自测(3 组用例通过) -> 入库 -> 热加载", DIM),
        ('  {"ok": true, "path": "generated", "skill": "repeat_text", "output": "ababab"}', OK),
        ("  端到端 7.8s，无需重启服务", OK),
    ], 2600))

    frames.append(([
        ("$ autoskill skills", PROMPT),
        ("", FG),
        ("  repeat_text  v1  active  fail_streak=0", FG),
        ("", FG),
        ('  $ python -c "...注入 bug 到 v1/impl.py..."   # 模拟线上事故', DIM),
        ("$ autoskill call repeat_text   # ×3", PROMPT),
        ("  第1次: ok=False  err=ValueError: injected bug", BAD),
        ("  第2次: ok=False  err=ValueError: injected bug", BAD),
        ("  第3次: ok=False  err=ValueError: injected bug", BAD),
    ], 3000))

    frames.append(([
        ("$ autoskill call repeat_text   # 第 4 次", PROMPT),
        ('  {"ok": false, "error": "技能已熔断隔离（连续失败 3 次），等待自修复升级"}', BAD),
        ("", FG),
        ("$ autoskill skills", PROMPT),
        ("  repeat_text  v1  quarantined  fail_streak=3", BAD),
    ], 2600))

    frames.append(([
        ('# 同类需求 -> 触发自修复', PROMPT),
        ("", FG),
        ("  命中隔离技能 repeat_text -> 生成候选 v2 -> 影子验证通过 -> 热替换", DIM),
        ('  {"ok": true, "detail": {"version": 2, "shadow_gate": true}}', OK),
        ("  影子门禁：候选未通过则自动回滚保持原版本（docs/ARCHITECTURE.md §5）", DIM),
    ], 2800))

    frames.append(([
        ("$ autoskill skills", PROMPT),
        ("  repeat_text  v2  active  history=2", OK),
        ("", FG),
        ("$ autoskill call repeat_text", PROMPT),
        ('  {"ok": true, "output": "ababab"}', OK),
        ("", FG),
        ("  ✅ 服务自愈完成：熔断 -> 自修复 -> 影子验证 -> 热替换", OK),
    ], 3200))

    images = [frame(lines) for lines, _ in frames]
    durations = [ms for _, ms in frames]
    out_path = out_dir / "demo.gif"
    images[0].save(out_path, save_all=True, append_images=images[1:], duration=durations, loop=0)
    size_kb = out_path.stat().st_size // 1024
    print(f"GIF 已生成: {out_path} ({size_kb} KB, {len(images)} 帧)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
