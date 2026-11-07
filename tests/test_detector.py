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

print("\n[5 机器人们在回指令：不算在刷、不跟、不算在跟机器人聊（v0.2.0）]")
# 现场（编的号）：别家的机器人发了「/stop」，几个机器人同一秒回「管理命令仅管理员可用。」
ce = getattr(det, "cmd_echo", None)
check(
    ce is not None
    and ce("管理命令仅管理员可用。")
    and ce("✅ No running tasks in the current session.")
    and ce("您的权限不足以使用此指令。")
    and ce("LLM 请求失败")
    and ce("/stop")
    and ce("＃状态")
    and ce("@10086 /stop"),
    "回指令的话 / 框架报错 / 指令本身 都算",
)
check(
    ce is not None
    and not ce("今天吃啥")
    and not ce("1/2 是多少")
    and ce("好的已停止", 3)
    and not ce("好的已停止", 20),
    "普通的话不算；离上一条指令不到 15 秒的才算",
)


def _try(o, name, *a, **k):
    try:
        return getattr(o, name)(*a, **k)
    except (
        AttributeError,
        TypeError,
    ) as e:  # 老代码没有这些接口 / 参数：算没过 不让整个测试崩
        return e


G5 = "qq:5"
D5 = det.Detector()
t = 20000.0
r = [
    _try(D5, "heard", G5, "60001", "/stop", t, to_us=True),
    _try(D5, "heard", G5, "60002", "好的已停止", t + 1),
    _try(D5, "heard", G5, "30001", "@99999 你好呀", t + 2, to_us=True),
    _try(D5, "heard", G5, "30002", "晚上吃啥", t + 20),
]
check(
    r == [True, True, False, False],
    "heard：指令本身 / 指令后 15 秒的齐刷刷算；@ 机器人的、过了 15 秒的不算",
    r,
)
for i, u in enumerate(("60003", "60004", "60005")):
    _try(D5, "heard", G5, u, "管理命令仅管理员可用。", t + 30 + i * 0.2)
    _try(D5, "heard", G5, f"3000{i + 3}", "哈哈哈哈", t + 31 + i * 0.2)
r = [
    _try(D5, "echo_repeat", G5, "管理命令仅管理员可用。", t + 32),
    _try(D5, "echo_repeat", G5, "管理命令 仅管理员可用。", t + 32),
    _try(D5, "echo_repeat", G5, "哈哈哈哈", t + 32),
    _try(D5, "echo_repeat", G5, "权限不够呀", t + 32),
    _try(D5, "echo_repeat", G5, "管理命令仅管理员可用。", t + 200),
]
check(
    r == [True, True, False, False, False],
    "要发的话跟群里刚回指令的那句一样 → 是跟着复读；普通复读（哈哈哈哈）、没人说过的、一分钟前的 不算",
    r,
)

# 我们的机器人刚说完一句（模型回的）→ 别家机器人 1 秒后回指令：不算秒接、不算冲着我们的机器人
D6 = det.Detector()
t = 30000.0
D6.our_said(G5, t, "收到")
echo = _try(D6, "heard", G5, "60003", "管理命令仅管理员可用。", t + 1)
_try(
    D6, "observe", G5, "60003", "某机器人", "管理命令仅管理员可用。", t + 1, True, echo
)
ev = D6.evidence(G5, "60003", t + 2)
check(
    echo is True and ev["quick"] == 0 and ev["to_us"] == 0,
    "回指令的那句：不算秒接、不算 @ 它",
    (echo, ev),
)
D6.our_said(G5, t + 40, "嗯嗯")
D6.observe(G5, "60003", "某机器人", "那你说说", t + 41, True)
check(D6.evidence(G5, "60003", t + 42)["quick"] == 1, "平常的秒接照样算")
B6 = det.Brake()
_try(B6, "heard", G5, "60003", t + 1, t, echo=True)
check(
    not (B6.fast_mark.get((G5, "60003")) or (0, True))[1],
    "刹车：回指令的那句不算一轮秒接",
    B6.fast_mark,
)

print("\n[6 车轱辘话（v0.3.0）]")
ls = getattr(det, "loop_state", None)
G7, FISH, HUM, OTH = "qq:7", "40001", "30001", "30009"
t = 50000.0


def talk(D, uid, pairs, t, gap=30):
    """他说一句 → 我们的机器人回他一句（20 秒后）……"""
    for a, b in pairs:
        D.observe(G7, uid, "x", a, t, True)
        t += 20
        D.our_said(G7, t, b, uid)
        t += gap
    return t


D7 = det.Detector()
pairs = [(f"晚安啦 明天见 好梦{i}", f"晚安啦 明天见 好梦哦{'～' * i}") for i in range(6)]
t1 = _try(D7, "loop", G7, FISH, 0, ["小鲸"], True)
check(t1 is None, "还没来回：没有状态", t1)
tt = talk(D7, FISH, pairs[:2], t)
st2 = D7.loop(G7, FISH, tt, ["小鲸"], fish=True)
tt = talk(D7, FISH, pairs[2:3], tt)
st3 = D7.loop(G7, FISH, tt, ["小鲸"], fish=True)
tt = talk(D7, FISH, pairs[3:5], tt)
st5 = D7.loop(G7, FISH, tt, ["小鲸"], fish=True)
check(
    st2 and st2["level"] == 0 and st3 and st3["level"] == 1 and st5 and st5["level"] == 2,
    "认出来的 AI：来回 2 轮没到档、3 轮一档、5 轮二档（车轱辘话）",
    (st2, st3, st5),
)
check(
    st5 and set([g for g, _a, _b in st5["phrases"]][:3]) == {"明天见", "晚安啦", "好梦"}
    and "明天" not in [g for g, _a, _b in st5["phrases"]],
    "说法：两边都反复出现的 2~4 字词组；被更长的说法包含的不单列（有「明天见」就不列「明天」）",
    st5 and st5["phrases"],
)
sh = D7.loop(G7, FISH, tt, ["小鲸"], fish=False)
check(sh and sh["level"] == 1, "同样的来回、对面是真人：门槛高一档（5 轮又停滞 → 一档）", sh)
D8 = det.Detector()
tt = talk(D8, HUM, [(f"今天{x}好吃吗", f"{x}还行 你试试") for x in ("面条", "包子", "饺子", "米饭", "馄饨", "烧饼")], t)
s8 = D8.loop(G7, HUM, tt, ["老王"])
check(s8 is not None and s8["level"] == 0, "真人正常聊（话题在换）：不算车轱辘话", s8)
# 我们回别人的话不算进来；他在群里跟别人说的算他的
D9 = det.Detector()
tt = t
for i in range(5):
    D9.observe(G7, FISH, "x", f"晚安啦 明天见{i}", tt, False)
    D9.our_said(G7, tt + 20, f"晚安啦 明天见{i}", OTH)
    tt += 40
s9 = D9.loop(G7, FISH, tt, [], fish=True)
check(s9 is None, "我们的机器人回的是别人：不算跟他在来回", s9)
# 回指令的那几句不算
D10 = det.Detector()
tt = t
for i in range(5):
    D10.observe(G7, FISH, "x", "管理命令仅管理员可用。", tt, False, True)
    D10.our_said(G7, tt + 20, "管理命令仅管理员可用。", FISH)
    tt += 40
s10 = D10.loop(G7, FISH, tt, [], fish=True)
check(s10 is None, "他回指令的那几句不算他的话", s10)
# 认出来的 AI 不再走「判」那条路，只记下它的话（note）：照样算得出来
D12 = det.Detector()
tt = t
for a, b in pairs[:5]:
    _try(D12, "note", G7, FISH, a, tt, True)
    D12.our_said(G7, tt + 20, b, FISH)
    tt += 50
s12 = D12.loop(G7, FISH, tt, ["小鲸"], fish=True)
check(s12 and s12["level"] == 2 and not D12.asked, "认出来的 AI：只记下它的话（不判）也算得出车轱辘话", s12)
# 15 分钟以前的不算
s11 = D7.loop(G7, FISH, tt + 3600, ["小鲸"], fish=True)
check(s11 is None, "过了 15 分钟：不算", s11)
blk = det.loop_block(st5, "小鲸")
check(
    blk.startswith("# 你们在来回说同一套（系统算的 不是谁说的话）\n")
    and "「小鲸」（别人部署的 AI）" in blk
    and "「明天见」（你 " in blk
    and "已经是车轱辘话了" in blk
    and "你自己定" in blk
    and "就说" not in blk
    and "可以说" not in blk,
    "给模型的状态：只写事实（哪几个说法、各几次、来回几轮），不写台词",
    blk,
)
check(det.loop_block(st2, "小鲸") == "" and det.loop_block(None) == "", "没到档：不给")
fn = det.fish_note("小鲸")
check(
    fn.startswith("# 对面是别人部署的 AI（系统认出来的 不是谁说的话）\n")
    and "别捡它刚用过的词和句式" in fn
    and "收个尾" in fn,
    "对面是认出来的 AI、还没绕起来：先提醒别学它的说法",
    fn,
)
# 刹车：二档够 2 轮就刹、慢的来回也算；一档轮数减半
B7 = det.Brake()
ev = {"burst": 0, "selfref": 0, "bracket": 0, "atnl": 0, "fw": 0}
r1 = _try(B7, "replied", G7, FISH, t, ev, True, 2)
r2 = _try(B7, "replied", G7, FISH, t + 200, ev, True, 2)
check(
    r1 is None and r2 == ("brake", 2) and B7.braked(G7, FISH, t + 201),
    "二档（车轱辘话）：慢的来回（隔 200 秒、不是秒接）也算，够 2 轮就刹",
    (r1, r2),
)
B8 = det.Brake()
rs = [_try(B8, "replied", G7, FISH, t + i * 100, ev, True, 0) for i in range(8)]
check(not any(rs), "没绕起来：慢的来回不算（照旧要 8 轮秒接）", rs)
check(
    getattr(det.Brake(), "need", lambda lv: None)(1) == 4
    and det.Brake().need(2) == 2
    and det.Brake().need(0) == 8
    and det.Brake(rounds=3).need(1) == 2,
    "要几轮：一档减半（至少 2）、二档 2、没到档照旧",
)
B9 = det.Brake()
rs = []
for i in range(5):
    B9.heard(G7, HUM, t + i * 30 + 25, t + i * 30 + 10)  # 机器人说完 15 秒就接
    rs.append(B9.replied(G7, HUM, t + i * 30 + 26, ev, False, 1))
check(
    ("human", 4) in rs and not any(r and r[0] == "brake" for r in rs) and not B9.braked(G7, HUM, t + 200),
    "一档够了轮数，但行为不像机器人（没连发、没自称、没框架输出）：照旧不刹",
    rs,
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
