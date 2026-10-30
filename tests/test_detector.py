#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""detector.py 单独测（不用装 AstrBot）：python tests/test_detector.py [插件目录 默认上一层]"""

import importlib.util
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
spec = importlib.util.spec_from_file_location("detector", d / "detector.py")
det = importlib.util.module_from_spec(spec)
spec.loader.exec_module(det)
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(
        ("  [过]   " if c else "  [没过] ")
        + m
        + ("" if c or not why else f"\n           {str(why)[:400]}")
    )


print("[1 认得出的样子]")
check(
    det.MARKUP.search("<h>这不是本小姐吗</h>？") and det.MARKUP.search("</think>好的"),
    "漏出来的标签",
)
check(
    not det.MARKUP.search("<think>标签怎么去掉")
    and not det.MARKUP.search("怎么把 <think></think> 去掉"),
    "真人问 <think> 标签不算",
)
check(
    det.SELFISH.search("本小姐可不吃这套")
    and det.SELFISH.search("本喵才不管")
    and not det.SELFISH.search("小姐姐好"),
    "本小姐 / 本喵算 小姐姐不算",
)
check(
    det.Detector.parse(
        '好的 {"bot": true, "mixed": false, "confidence": 72, "why": "秒接"}'
    )
    == (True, 72, "秒接", False)
    and det.Detector.parse("看不懂") == (False, 0, "", False),
    "模型的回答解析（带废话也行 看不懂当真人 0）",
)

print("\n[2 证据多了就再看]")
D = det.Detector()
G, W, H = "qq:1", "40001", "30001"
t = 1000.0
lines = [
    "叫谁孩子呢",
    "先说说那一位",
    "这么急",
    "才两句就破防",
    "骂谁呢",
    "本小姐可不吃这套",
    "老爸你骂得最凶",
    "本小姐可是干净鱼",
    "反正你也就嘴上凶",
    "就这？骂都骂不利索",
]
checks = []
for i, x in enumerate(lines):
    t += 9 if i % 2 else 2
    if D.observe(G, W, "隔壁的座头鲸", x, t) == "check":
        checks.append(t)
        D.decide(W, "隔壁的座头鲸", G, (False, 55, "口语", False), t)
check(len(checks) == 1, "零星几句：看一次（模型说像真人 55）", checks)
t += 60
D.observe(G, W, "隔壁的座头鲸", "<h>这不是本小姐吗</h>？", t)
again = None
for k in range(10):
    t += 5
    D.our_said(G, t, "大早上就吵")
    for j, gap in enumerate((14, 2)):
        t += gap
        if (
            D.observe(
                G, W, "隔壁的座头鲸", f"第{k}轮第{j}句 本小姐{'' if j else '可不服'}", t
            )
            == "check"
            and again is None
        ):
            again = (k + 1, t - checks[0])
check(
    again and again[0] <= 4,
    f"对骂第 {again and again[0]} 轮就再看（离第一次 {again and int(again[1])} 秒）",
    again,
)
ev = D.evidence(G, W, t)
check(
    ev["quick"] >= 8 and ev["burst"] >= 8 and ev["selfref"] >= 2 and ev["fw"] >= 1,
    "行为：秒接 / 连发 / 自称 / 标签都算上",
    ev,
)
kind, _, why = D.decide(W, "隔壁的座头鲸", G, (True, 72, "秒接连发", False), t)
check(kind == "mark", "模型说 AI 把握 72 + 行为分 ≥8 → 标", why)
check(
    D.decide("50001", "路人", G, (True, 72, "工整", False), t)[0] == "unsure",
    "行为分不够 → 不标（unsure）",
)
check(
    D.decide("50002", "某号", G, (True, 90, "混用", True), t)[0] == "mixed",
    "混用号不标",
)

print("\n[3 跟机器人聊得快的真人：不算连发、不刹]")
D2 = det.Detector()
B = det.Brake()
t = 5000.0
res = []
for i in range(14):
    D2.our_said(G, t, "嗯嗯")
    t += 1.5  # 机器人回得快 他也接得快（一句一句）
    B.heard(G, H, t, D2.last_said(G))
    D2.observe(
        G,
        H,
        "老王",
        ["哈哈", "你说的对", "不对吧", "我想想", "行吧"][i % 5] + str(i),
        t,
        to_us=True,
    )
    t += 0.5
    res.append(B.replied(G, H, t, D2.evidence(G, H, t, B.window)))
ev = D2.evidence(G, H, t)
check(ev["burst"] == 0, "两句之间机器人说过话的不算连发", ev)
check(
    not any(r and r[0] == "brake" for r in res),
    "来回 14 轮也不刹（像真人）",
    [r for r in res if r],
)

print("\n[4 刹车：机器人一说完就接两条]")
D3 = det.Detector()
B3 = det.Brake()
t = 9000.0
got = None
for i in range(12):
    D3.our_said(G, t, "哼")
    t += 15
    B3.heard(G, W, t, D3.last_said(G))
    D3.observe(G, W, "某某鲸", f"本小姐第{i}轮", t, to_us=True)
    t += 2
    D3.observe(G, W, "某某鲸", f"不服第{i}轮", t)
    r = B3.replied(G, W, t + 1, D3.evidence(G, W, t + 1, B3.window))
    t += 1
    if r and r[0] == "brake":
        got = i + 1
        break
check(got == 8, f"她说完 15 秒接、两条连发：第 {got} 轮刹住", got)
check(
    B3.braked(G, W, t + 5) and not B3.braked(G, W, t + 700), "刹 10 分钟 之后自己放开"
)

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print(
    "\n"
    + "=" * 50
    + f"\n  结果：{ok} 项过了"
    + (f"，{len(bad)} 项没过" if bad else "，全过")
)
for m in bad:
    print("    -", m)
sys.exit(1 if bad else 0)
