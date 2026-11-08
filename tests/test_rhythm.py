#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""群里说话的节奏（v0.4.0）单独测（不用装 AstrBot）：python tests/test_rhythm.py [插件目录 默认上一层]
  1 不对称分：编时间序列 —— 只在别人说完十来秒接话的机器人 vs 有快有慢、会自己起话头的真人
  2 已知机器人 / 真人上分不分得开（简单统计）
  3 真人信号：主动 @ 好几个不同的人 / 跟别人来回 / 冷场后开话题（各有反例）
  4 真人信号一票否决（接上 detector.py）：秒接多但 @ 过三个不同的人的号 → 不自动标，只记「观察」；模型把握 ≥ 75 照标
  5 不对称分影子模式：记下「要是算进去会怎样」；打开参与判断以后真的加分"""

import importlib.util
import random
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(
        ("  [过]   " if c else "  [没过] ")
        + m
        + ("" if c or not why else f"\n           {str(why)[:400]}")
    )


def done():
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


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _try(o, name, *a, **k):
    try:
        return getattr(o, name)(*a, **k)
    except (AttributeError, TypeError) as e:  # 老代码没有这些接口 / 参数：算没过，不让整个测试崩
        return e


det = load("detector", d / "detector.py")
try:
    rh = load("rhythm", d / "core" / "rhythm.py")
except Exception as e:  # 老版本没有这个文件：算没过
    check(False, "有 core/rhythm.py（不对称分、真人信号）", e)
    done()

G = "qq:7001"
BOT, HUM, A, B, C = "40004", "30003", "20002", "50005", "60006"
rnd = random.Random(7)

print("[1 不对称分：编时间序列]")
R = rh.Rhythm()
t = 100000.0
# 机器人：只在别人（真人 / 你的机器人）说完 8~14 秒接一句，冷场了也不说话
for i in range(14):
    R.add(G, A if i % 3 else rh.SELF, t)
    t += rnd.uniform(8, 14)
    R.add(G, BOT, t)
    t += rnd.uniform(20, 60)
    if i % 5 == 4:
        t += 900  # 冷场 15 分钟：机器人不说话，下一句是别人起的头
fb = R.asym(G, BOT, t)
# 真人：有时 2 秒就接、有时一分多钟才接；冷场以后自己起话头；接着自己上一句说
t2 = t + 3600
for i in range(14):
    if i % 4 == 0:
        t2 += 700  # 冷场以后他先开口
        R.add(G, HUM, t2)
        t2 += 3
        R.add(G, HUM, t2)  # 接着自己说：不算新的一次出声
    else:
        R.add(G, B, t2)
        t2 += [2, 75, 9, 40, 110, 4][i % 6]
        R.add(G, HUM, t2)
    t2 += rnd.uniform(30, 90)
fh = R.asym(G, HUM, t2)
check(
    fb and fb["react"] >= 0.9 and fb["init"] == 0 and fb["spread"] is not None and fb["spread"] <= 0.4,
    f"机器人的三个特征：几乎都是别人说完 30 秒内接、从不冷场后起话头、接话快慢很固定（{fb}）",
    fb,
)
check(
    fh and fh["init"] >= 0.15 and fh["spread"] is not None and fh["spread"] >= 0.8 and fh["react"] <= 0.6,
    f"真人的三个特征：会冷场后起话头、接话有快有慢（{fh}）",
    fh,
)
check(
    fb and fh and fb["score"] >= 80 and fh["score"] <= 30,
    f"不对称分：机器人 {fb and fb['score']}、真人 {fh and fh['score']}（越高越像机器人）",
)
check(fb and fb["turns"] == 14, "接着自己上一句说的不算新的一次出声；时间线第一句不算（前面是什么不知道）", fb)
R2 = rh.Rhythm()
for i in range(5):
    R2.add(G, A, 1000 + i * 60)
    R2.add(G, BOT, 1010 + i * 60)
check(R2.asym(G, BOT, 2000) is None, "出声不到 6 次：不算分（几句话看不出节奏）")
R3 = rh.Rhythm()
for i in range(10):
    R3.add(G, A, 1000 + i * 60, echo=True)
    R3.add(G, BOT, 1001 + i * 60, echo=True)
check(R3.asym(G, BOT, 2000) is None, "回指令的那几句不算（v0.2.0：那是机器人们在回指令，不是在聊天）")
check(R.asym(G, BOT, t + 7 * 3600) is None, "只看最近 6 小时")
check(
    rh.asym_bonus(75) == rh.ASYM_BONUS == 4 and rh.asym_bonus(69) == 0 and rh.asym_bonus(None) == 0,
    "要是参与判断：分 ≥ 70 的行为分 +4",
)
txt = rh.asym_text(fb)
check(txt.startswith(f"{fb['score']}（出声 14 次") and "冷场后起话头 0%" in txt, "日志 / 面板上的一句话", txt)

print("\n[2 已知的机器人 / 真人上分不分得开]")
s = rh.separation([90, 85, 80, 72], [20, 35, 40])
check(s["auc"] == 1.0 and s["verdict"] == "分得开" and s["bot_hit"] == 4 and s["human_hit"] == 0, "分得开：AUC 1.0", s)
s = rh.separation([60, 30, 50], [55, 35, 45, 75])
check(s["auc"] is not None and s["auc"] < 0.7 and s["verdict"] == "分不开" and s["human_hit"] == 1, "分不开：机器人、真人的分搅在一起；真人里有 1 个 ≥ 70（开了会误伤）", s)
s = rh.separation([90, 85], [20])
check(s["auc"] is None and s["verdict"].startswith("样本不够"), "各不到 3 个：样本不够，不下结论", s)

print("\n[3 真人信号]")
Rs = rh.Rhythm()
t = 200000.0
# 他 @ 了三个这阵子没说话的人（主动找人）
for who in (A, B, C):
    Rs.add(G, HUM, t, ats=(who,))
    t += 50
sig = dict(Rs.signals(G, HUM, t))
check("at" in sig and "3 个不同的人" in sig["at"], "主动 @ 过 3 个不同的人 → 真人信号", sig)
# 机器人回人：@ 的都是刚说过话、叫它的那个人
Rb = rh.Rhythm()
t = 200000.0
for who in (A, B, C, A, B):
    Rb.add(G, who, t)
    t += 9
    Rb.add(G, BOT, t, ats=(who,))
    t += 60
check("at" not in dict(Rb.signals(G, BOT, t)), "@ 的是刚说过话的人（机器人回人也 @）：不算主动 @")
Rx = rh.Rhythm()
for i, who in enumerate((A, B)):
    Rx.add(G, HUM, 1000 + i * 50, ats=(who, rh.SELF))
check("at" not in dict(Rx.signals(G, HUM, 2000)), "只 @ 了 2 个人（你的机器人不算）：不够")
# 跟别人来回：没找你的机器人、对面不是机器人
Rt = rh.Rhythm()
t = 300000.0
for _ in range(4):
    Rt.add(G, B, t)
    t += 20
    Rt.add(G, HUM, t)
    t += 30
sig = dict(Rt.signals(G, HUM, t))
check("talk" in sig and B in sig["talk"], "跟同一个人来回聊了 3 轮以上（都没找你的机器人）→ 真人信号", sig)
check(
    "talk" not in dict(Rt.signals(G, HUM, t, is_bot=lambda u: u == B)),
    "对面是认出来的机器人：不算（那是两个机器人 / 人跟机器人在聊）",
)
Rt2 = rh.Rhythm()
t = 300000.0
for _ in range(4):
    Rt2.add(G, B, t, to_us=True)
    t += 20
    Rt2.add(G, HUM, t)
    t += 30
check("talk" not in dict(Rt2.signals(G, HUM, t)), "对面那几句是在找你的机器人：不算「跟机器人无关的对话」")
Rt3 = rh.Rhythm()
t = 300000.0
for _ in range(4):
    Rt3.add(G, rh.SELF, t)
    t += 20
    Rt3.add(G, HUM, t)
    t += 30
check("talk" not in dict(Rt3.signals(G, HUM, t)), "跟你的机器人来回：不算")


def back_and_forth(p_kw, x_kw, rounds=4):
    """真人 B 说一句（带 p_kw）→ 别家 AI 回一句（带 x_kw），来回 rounds 轮"""
    R_ = rh.Rhythm()
    t_ = 300000.0
    try:
        for _ in range(rounds):
            R_.add(G, B, t_, **p_kw)
            t_ += 20
            R_.add(G, BOT, t_, **x_kw)
            t_ += 30
    except TypeError as e:  # 老代码不记「引用了谁」：算没过
        return {"talk": f"出错 {e}"}, R_, t_
    return dict(R_.signals(G, BOT, t_)), R_, t_


# 11-08 复查：别家 AI 被真人 @ / 引用着聊 4 个来回，以前就凑够「跟别人来回」了
sig, _r, _t = back_and_forth({"ats": (BOT,)}, {"ats": (B,)})
check("talk" not in sig, "别家 AI 被真人 @ 着聊 4 个来回（它回人也 @ 对方）：不算「自己找别人来回」", sig)
sig, _r, _t = back_and_forth({"refs": (BOT,)}, {})
check("talk" not in sig, "被真人引用着聊 4 个来回：也不算", sig)
sig, _r, _t = back_and_forth({"ats": (C,)}, {})
check("talk" not in sig, "对面那几句在 @ 第三个人（不是在跟他聊）：不算", sig)
sig, _r, _t = back_and_forth({}, {"ats": (C,)})
check("talk" not in sig, "他那几句在 @ 第三个人：不算", sig)
sig, _r, _t = back_and_forth({}, {"ats": (B,)})
check("talk" in sig, "他自己 @ 对面、对面没 @ 谁（他在主动找人聊）：算", sig)
# 冷场很久以后自己开话题（11-08 复查：带「冷场试探」的机器人冷场 5 分钟就开口，以前开 2 次就算）
Ri = rh.Rhythm()
t = 400000.0
for _ in range(4):  # 活人感那种：在线时冷场 5~6 分钟试探一句，一天好几回
    Ri.add(G, A, t)
    t += 330
    Ri.add(G, BOT, t)
    t += 3600
check("init" not in dict(Ri.signals(G, BOT, t)), "冷场 5~6 分钟就开口（冷场试探），开了 4 次：不算", Ri.signals(G, BOT, t))
Ri2 = rh.Rhythm()
t = 400000.0
for _ in range(2):
    Ri2.add(G, A, t)
    t += 25 * 60
    Ri2.add(G, HUM, t)
    t += 60
check("init" not in dict(Ri2.signals(G, HUM, t)), "冷场 20 分钟以上开口 2 次（可能是早安 / 晚安这种定时推送）：不够")
Ri3 = rh.Rhythm()
t = 400000.0
for _ in range(3):
    Ri3.add(G, A, t)
    t += 25 * 60
    Ri3.add(G, HUM, t)
    t += 3 * 3600  # 三次分散在大半天里：只看 6 小时就数不到 3 次
sig = dict(Ri3.signals(G, HUM, t))
check("init" in sig and "20 分钟以上" in sig["init"], "冷场 20 分钟以上、最近一天自己开口 3 次 → 真人信号", sig)
check("init" not in dict(Ri3.signals(G, HUM, t + 20 * 3600)), "一天以前的不算")

print("\n[4 真人信号一票否决（接上 detector）]")


def busy_talker(D, Rr, uid, ats_people=()):
    """编一个号：你的机器人每说一句，他 14 秒内就接、再 2 秒连发一条（秒接多、连发多 → 行为分高）；
    ats_people：开头先 @ 这几个人（他们这阵子没说过话）"""
    t = 500000.0
    for p in ats_people:
        D.observe(G, uid, "阿木", f"@{p} 你作业交了没", t, False)
        Rr.add(G, uid, t, ats=(p,))
        t += 40
    for k in range(10):
        t += 5
        D.our_said(G, t, f"嗯嗯第{k}句")
        Rr.add(G, rh.SELF, t)
        for j, gap in enumerate((14, 2)):
            t += gap
            D.observe(G, uid, "阿木", f"第{k}轮第{j}句哈哈", t, j == 0)
            Rr.add(G, uid, t, to_us=(j == 0))
    return t


D1, R1 = det.Detector(), rh.Rhythm()
t = busy_talker(D1, R1, HUM)
ev = D1.evidence(G, HUM, t)
k0 = D1.decide(HUM, "阿木", G, (True, 70, "秒接连发", False), t)[0]
check(ev["score"] >= 8 and k0 == "mark", f"对照：没有真人信号、模型说 AI 把握 70 + 行为分 {ev['score']} → 标（老规矩）", (ev, k0))

D2, R2b = det.Detector(), rh.Rhythm()
t = tD2 = busy_talker(D2, R2b, HUM, ats_people=(A, B, C))
veto = R2b.signals(G, HUM, t)
r = _try(D2, "decide", HUM, "阿木", G, (True, 70, "秒接连发", False), t, veto=veto)
kind, why = (r[0], r[2]) if isinstance(r, tuple) else (r, "")
v = D2.store["verdicts"].get(HUM) or {}
check(
    [k for k, _ in veto] == ["at"] and kind == "watch",
    "秒接多、行为分够，但 @ 过三个不同的人 → 不自动标，只记「观察」",
    (veto, r),
)
check(
    "主动 @ 过 3 个不同的人" in str(why) and "不自动标" in str(why) and "主动 @" in str((v.get("watch") or {}).get("why")),
    "说明里写清楚是哪条信号否决的；判断记录里留着「观察」（面板看得到）",
    (why, v),
)
r = _try(D2, "decide", HUM, "阿木", G, (True, 80, "秒接连发", False), t, veto=veto)
check(isinstance(r, tuple) and r[0] == "mark" and "watch" not in D2.store["verdicts"][HUM], "模型把握 ≥ 75：照标（一票否决只管「光靠行为分」那几条路）", r)
r = _try(D2, "decide", HUM, "阿木", G, (False, 60, "像真人", True), t, veto=veto)
check(isinstance(r, tuple) and r[0] == "mixed", "混用号照旧不标", r)
D3 = det.Detector()
t = 600000.0
for i in range(4):
    D3.observe(G, BOT, "某号", f"@某人\n第{i}句", t, False)
    t += 30
r = _try(D3, "decide", BOT, "某号", G, (False, 40, "像真人", False), t, veto=[("init", "冷场后自己开过 3 次话题")])
check(
    isinstance(r, tuple) and r[0] == "mark" and "watch" not in D3.store["verdicts"][BOT],
    "框架回复格式那条路（@人后直接换行 ≥ 3 次）不归一票否决管：有真人信号也照标（11-08 复查）",
    r,
)
r = _try(D2, "decide", HUM, "阿木", G, (True, 70, "秒接连发", False), tD2, veto=veto)
_try(D2, "decide", HUM, "阿木", G, (True, 70, "秒接连发", False), tD2, veto=[])
check(isinstance(r, tuple) and r[0] == "watch" and "watch" not in D2.store["verdicts"][HUM], "没有真人信号了：照旧标；「观察」的记号清掉", D2.store["verdicts"][HUM])

print("\n[5 不对称分影子模式]")
D4 = det.Detector()
t = 700000.0
for k in range(4):  # 行为分不高（5 分上下）：「把握 ≥ 65 且行为分 ≥ 8」那条路够不上
    t += 5
    D4.our_said(G, t, "嗯")
    t += 14
    D4.observe(G, BOT, "某号", f"第{k}句", t, True)
    t += 2
    D4.observe(G, BOT, "某号", f"第{k}句又一句", t, False)
sc = D4.evidence(G, BOT, t)["score"]
r = _try(D4, "decide", BOT, "某号", G, (True, 68, "工整", False), t, asym=85, asym_plus=4, asym_on=False)
v = D4.store["verdicts"].get(BOT) or {}
check(
    4 <= sc < 8 and isinstance(r, tuple) and r[0] == "unsure" and v.get("asym") == 85 and v.get("asym_if") == "mark",
    f"影子模式：照旧不标（行为分 {sc} 不到 8），但记下「要是算进去（+4）→ 会标」",
    (sc, r, v),
)
r = _try(D4, "decide", BOT, "某号", G, (True, 68, "工整", False), t, asym=85, asym_plus=4, asym_on=True)
v = D4.store["verdicts"].get(BOT) or {}
check(
    isinstance(r, tuple) and r[0] == "mark" and "含不对称分 +4" in r[2] and v.get("asym_if") == "unsure",
    "打开「参与判断」：行为分 +4 够上了 → 标；记下「不算的话 → 不标」",
    r,
)
r = _try(D4, "decide", BOT, "某号", G, (True, 68, "工整", False), t, asym=50, asym_plus=0, asym_on=True)
check(isinstance(r, tuple) and r[0] == "unsure" and D4.store["verdicts"][BOT].get("asym_if") == "unsure", "分不到 70：不加分，两边一样", r)
r = _try(D4, "decide", BOT, "某号", G, (True, 68, "工整", False), t)
check(isinstance(r, tuple) and "asym" not in D4.store["verdicts"][BOT], "没算出分（数据不够）：不记", D4.store["verdicts"][BOT])
check(getattr(det, "KIND_TEXT", {}).get("watch", "").startswith("只记「观察」"), "结果的大白话（日志 / 面板用）")

print("\n[6 面板要所有人的不对称分：一个群扫一遍、60 秒缓存（11-08 复查）]")
fa, ba = getattr(rh, "features_all", None), getattr(rh.Rhythm, "best_all", None)
if fa is None or ba is None:
    check(False, "有 features_all / Rhythm.best_all（一个群扫一遍、带缓存）")
else:
    import time as _time

    RP = rh.Rhythm()
    now = 900000.0
    for g in range(100):  # 100 个群 × 800 句、每群 40 个人
        gk = f"qq:{7000 + g}"
        t = now - 5 * 3600
        for i in range(800):
            t += rnd.uniform(2, 40)
            RP.add(gk, rh.SELF if i % 7 == 0 else str(20000 + rnd.randrange(40)), t)
    seq0 = RP.seq("qq:7000", now)
    one = {u: rh.features(seq0, u) for u in {x[1] for x in seq0} - {rh.SELF}}
    one = {u: f for u, f in one.items() if f}
    check(fa(seq0) == one, f"一遍扫完算的，跟挨个人算的一模一样（{len(one)} 个人）")
    def old_way():  # 老办法：每个人各把整个群扫一遍
        out = {}
        for gk in list(RP.tl):
            s_ = RP.seq(gk, now)
            for u in {x[1] for x in s_} - {rh.SELF}:
                f = rh.features(s_, u)
                if f and (u not in out or f["turns"] > out[u]["turns"]):
                    out[u] = f
        return out

    def timed(fn):  # 机器忙的时候计时会抖：各跑 3 次取最快的
        best_t, got = 9e9, None
        for _ in range(3):
            t0 = _time.perf_counter()
            got = fn()
            best_t = min(best_t, _time.perf_counter() - t0)
        return best_t, got

    t_old, old = timed(old_way)
    t_new, new = timed(lambda: RP.best_all(now, max_age=0))
    check(new == old, "所有人的分：结果跟老办法一样")
    check(
        t_new * 2 < t_old and t_new < 0.5,
        f"100 个群 × 800 句、每群 40 人：老办法 {t_old:.2f} 秒、现在 {t_new:.2f} 秒（快一倍以上，不卡事件循环）",
    )
    new = RP.best_all(now)
    RP.add("qq:7000", "20001", now + 1)
    t0 = _time.perf_counter()
    again = RP.best_all(now + 10)
    t_hit = _time.perf_counter() - t0
    check(again is new and t_hit < 0.01, "60 秒内再要：直接给上次算好的（面板 15 秒刷一次）")
    check(RP.best_all(now + 61) is not new, "过了 60 秒：重新算")

done()
