# -*- coding: utf-8 -*-
"""
群里说话的「节奏」（v0.4.0，纯逻辑，不 import astrbot，单独能测）

每个群记一条时间线：谁、几点说的、@ 了谁、引用了谁、是不是冲着你的机器人、是不是在回指令（不记说了什么）。拿它算两样：

1. 不对称分（影子模式）—— 论文 ChatbotID（NeurIPS 2025）的思路：机器人和真人在群里的节奏不对称。
   机器人是被叫起来才说话的：出声几乎都在别人说完几秒内、几乎不在冷场后自己开新话题、
   接话的快慢很固定（大模型生成一句的时间差不多）；真人有快有慢、会自己起话头。
   每个人在每个群最近的出声 → 三个特征 → 0~100 的分（越高越像机器人）。
   先只记、不拿来判：看准了它在已知的机器人 / 真人上分得开，再在配置里打开「参与判断」（行为分 +ASYM_BONUS）。
2. 真人信号（一票否决「把握 ≥ 65 且行为分 ≥ 8」那条路）—— 11-07 作者那边一个真人被行为分标成了 AI。
   机器人几乎不会：主动 @ 好几个不同的人（它 @ 的是刚叫它的人）、自己找别人来回聊（不是被人 @ / 引用着聊）、
   冷场很久以后自己开话题。有这些的号，行为分再高也只记「观察」，不自动标（模型把握 ≥ 75 的照标）。

时间线只在内存里（重启从头算），每个群最近 TL_MAX 句、只看最近 WIN 秒（冷场开话题看一天）。
"""

import math
from collections import defaultdict, deque
from operator import itemgetter
from statistics import median

SELF = "@self"  # 时间线里「你的机器人」说的话（它说话的时间是别人接话快慢的起点）
TL_MAX = 800  # 每个群最多记这么多句
WIN = 6 * 3600  # 只看最近 6 小时
COLD = 300  # 群里这么久没人说话 = 冷场；冷场后第一句 = 开新话题
REACT = 30  # 别人说完这么多秒内出声 = 接话（大模型生成一句一般 3~20 秒）
LAT_MAX = 120  # 算「接话快慢的离散」只看 2 分钟内接上的（再慢就不算在接这句了）
MIN_TURNS = 6  # 出声不到这么多次：不算分（几句话看不出节奏）
MIN_LAT = 4  # 接话不到这么多次：离散算不准，这一项按一半算
ASYM_MIN = 70  # 不对称分 ≥ 这个数：要是参与判断，行为分加 ASYM_BONUS
ASYM_BONUS = 4
AT_FRESH = 180  # @ 的那个人这么多秒内说过话 = 在回他（机器人回人也会 @ 刚叫它的人）—— 不算「主动 @」
AT_MIN = 3  # 主动 @ 过这么多个不同的人 = 真人信号
TALK_GAP = 120  # 两句挨着、隔不到 2 分钟 = 在接对方的话
TALK_MIN = 3  # 跟同一个人（不是机器人）来回这么多轮 = 真人信号
# 冷场开话题（真人信号③）比不对称分的「冷场」要求严得多（11-08 复查）：
# 带「冷场试探」的机器人（鱼力max · 活人感默认：被叫起来以后在线 8~15 分钟，这期间冷场 5 分钟就可能试探一句）
# 开口前的冷场只有 5~15 分钟；20 分钟以上的冷场它碰不上。一天 3 次：一两次可能是早安 / 晚安这种定时推送
INIT_COLD = 20 * 60
INIT_MIN = 3
INIT_WIN = 24 * 3600
ASYM_CACHE = 60  # 面板 15 秒刷一次：所有人的不对称分 60 秒内重复要的直接给上次算好的


def _clamp(x):
    return max(0.0, min(1.0, float(x)))


def turns(seq, uid):
    """seq：按时间排好的 [(时间, 谁, @谁, 冲着你的机器人, 回指令, 引用了谁)] → 他每次「出声」：[("init" / "resp", 离上一句几秒)]
    冷场后的第一句 = init（开话题）；接在别人后面的第一句 = resp（接话）；接着自己上一句说的不算新的一次出声。
    时间线里第一句前面是什么不知道：不算"""
    out = []
    for a, b in zip(seq, seq[1:]):
        if b[1] != uid:
            continue
        gap = b[0] - a[0]
        if gap >= COLD:
            out.append(("init", gap))
        elif a[1] != uid:
            out.append(("resp", gap))
    return out


def asym_score(react, init, spread):
    """三个特征 → 0~100（越高越像机器人）。每项 0~1 平均：
    接话占比 ≥ 0.9 满、≤ 0.5 零；开话题占比 0 满、≥ 0.15 零；接话快慢的离散（ln 秒的标准差）≤ 0.4 满、≥ 1.1 零"""
    a1 = _clamp((react - 0.5) / 0.4)
    a2 = _clamp(1 - init / 0.15)
    a3 = 0.5 if spread is None else _clamp((1.1 - spread) / 0.7)
    return int(round(100 * (a1 + a2 + a3) / 3))


def features(seq, uid):
    """他在这段时间线里的三个特征和不对称分；出声不到 MIN_TURNS 次 → None"""
    return _feat(turns(seq, uid))


def features_all(seq):
    """一个群的时间线 → {谁: 特征}（出声不到 MIN_TURNS 次的不在里面）。
    一遍扫完：面板要所有人的分，挨个人各扫一遍是「人数 × 句数」，群多人多会卡住事件循环（11-08 复查）"""
    per = defaultdict(list)
    for a, b in zip(seq, seq[1:]):
        gap = b[0] - a[0]
        if gap >= COLD:
            per[b[1]].append(("init", gap))
        elif a[1] != b[1]:
            per[b[1]].append(("resp", gap))
    out = {}
    for u, ts in per.items():
        f = _feat(ts) if u != SELF else None
        if f:
            out[u] = f
    return out


def _feat(ts):
    n = len(ts)
    if n < MIN_TURNS:
        return None
    resp = [g for k, g in ts if k == "resp"]
    react = sum(1 for g in resp if g <= REACT) / n
    init = sum(1 for k, _ in ts if k == "init") / n
    # 用 ln(秒) 的标准差：8 秒和 16 秒的差别、跟 30 秒和 60 秒的差别一样大（快慢是按倍数比的）
    lat = [math.log(max(0.5, g)) for g in resp if g <= LAT_MAX]
    spread = round(_std(lat), 2) if len(lat) >= MIN_LAT else None
    return {
        "score": asym_score(react, init, spread),
        "turns": n,
        "react": round(react, 2),
        "init": round(init, 2),
        "spread": spread,
    }


def _std(xs):
    """总体标准差。不用 statistics.pstdev：它为了算得精确走分数，面板一次要算几千个人，太慢"""
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def asym_bonus(score):
    """不对称分要是参与判断：行为分加几分（不够 ASYM_MIN 或者没分 → 0）"""
    return ASYM_BONUS if score is not None and score >= ASYM_MIN else 0


def asym_text(f):
    """给日志 / 面板看的一句话"""
    if not f:
        return "数据不够"
    sp = "—" if f.get("spread") is None else f"{f['spread']:.2f}"
    return (
        f"{f['score']}（出声 {f['turns']} 次：别人说完 {REACT} 秒内就接 {round(f['react'] * 100)}%、"
        f"冷场后起话头 {round(f['init'] * 100)}%、接话快慢的离散 {sp}）"
    )


def separation(bots, humans):
    """已知的机器人 / 真人身上的不对称分 → 分得开不开（简单统计）：
    各自几个、中位数、AUC（随便挑一个机器人、一个真人，机器人分更高的概率；0.5 = 瞎猜）、
    分数 ≥ ASYM_MIN（参与判断时会加分）的各有几个 —— 真人里也有不少的话，开了会误伤"""
    bots = [int(x) for x in bots]
    humans = [int(x) for x in humans]
    nb, nh = len(bots), len(humans)
    out = {
        "bots": nb,
        "humans": nh,
        "bot_med": median(bots) if bots else None,
        "human_med": median(humans) if humans else None,
        "bot_hit": sum(1 for x in bots if x >= ASYM_MIN),
        "human_hit": sum(1 for x in humans if x >= ASYM_MIN),
        "auc": None,
        "verdict": "样本不够（机器人、真人各要至少 3 个有分的）",
    }
    if nb >= 3 and nh >= 3:
        wins = sum(1.0 if b > h else 0.5 if b == h else 0.0 for b in bots for h in humans)
        auc = wins / (nb * nh)
        out["auc"] = round(auc, 2)
        out["verdict"] = (
            "分得开" if auc >= 0.85 else "有点分得开" if auc >= 0.7 else "分不开"
        )
    return out


class Rhythm:
    """一个 AstrBot 里一份。时间都从外面传进来（测试用假时钟）"""

    def __init__(self, maxlen=TL_MAX):
        # 群 -> [(时间, 谁, @了谁, 冲着你的机器人, 在回指令, 引用了谁)]
        self.tl = defaultdict(lambda: deque(maxlen=maxlen))
        self._all = None  # (算的时间, {谁: 特征})：best_all 的缓存

    def add(self, gk, who, t, ats=(), to_us=False, echo=False, refs=()):
        """群里有人说了一句（你的机器人说的 who=SELF）。ats：@ 了谁、refs：引用了谁的话（都不含你的机器人、全体成员）"""
        self.tl[str(gk)].append(
            (
                float(t),
                str(who),
                tuple(str(a) for a in ats or ()),
                bool(to_us),
                bool(echo),
                tuple(str(a) for a in refs or ()),
            )
        )

    def seq(self, gk, now, win=WIN):
        """最近 win 秒按时间排好的；回指令的那几句不算（v0.2.0：那一阵是机器人们在回指令，不是在聊天）"""
        return sorted(
            (
                x
                for x in self.tl.get(str(gk)) or ()
                if 0 <= now - x[0] <= win and not x[4]
            ),
            key=itemgetter(0),
        )

    def speakers(self, gk, now, win=WIN):
        return {x[1] for x in self.seq(gk, now, win)} - {SELF}

    def asym(self, gk, uid, now, win=WIN):
        """他在这个群的不对称分和三个特征；数据不够 → None"""
        return features(self.seq(gk, now, win), str(uid))

    def best(self, uid, now, win=WIN):
        """他在哪个群出声最多，就用那个群的（一个号在好几个群）→ (群, 特征) / (None, None)"""
        got = (None, None)
        for gk in list(self.tl):
            f = self.asym(gk, uid, now, win)
            if f and (got[1] is None or f["turns"] > got[1]["turns"]):
                got = (gk, f)
        return got

    def best_all(self, now, max_age=ASYM_CACHE):
        """所有人现在的不对称分（每人取出声最多的那个群）→ {谁: 特征}。
        一个群扫一遍（features_all）；max_age 秒内再要直接给上次的（面板 15 秒刷一次，用不着每次都重算）"""
        c = self._all
        if c is not None and 0 <= now - c[0] < max_age:
            return c[1]
        best = {}
        for gk in list(self.tl):
            for u, f in features_all(self.seq(gk, now)).items():
                if u not in best or f["turns"] > best[u]["turns"]:
                    best[u] = f
        self._all = (now, best)
        return best

    def signals(self, gk, uid, now, is_bot=None, win=WIN):
        """他最近的真人信号 → [(哪种, 一句说明)]：at 主动 @ 过好几个不同的人 / talk 跟别人来回 / init 冷场很久以后开话题"""
        uid = str(uid)
        isb = is_bot or (lambda u: False)
        seq = self.seq(gk, now, win)
        out = []
        # ① 主动 @：@ 的人这阵子没说过话（不是在回他）。@ 你的机器人不算（找你的机器人聊天不是真人信号）
        last, tg = {}, []
        for x in seq:
            t, who, ats = x[0], x[1], x[2]
            if who == uid:
                for a in ats:
                    if a in (uid, SELF, "all") or a in tg:
                        continue
                    if t - last.get(a, float("-inf")) <= AT_FRESH:
                        continue
                    tg.append(a)
            last[who] = t
        if len(tg) >= AT_MIN:
            out.append(
                ("at", f"主动 @ 过 {len(tg)} 个不同的人（{'、'.join(tg[:4])}；他们那阵子都没说话，不是在回谁）")
            )
        # ② 自己找别人来回：一句接一句、隔不到 2 分钟，两边都没找你的机器人，对面也不是认出来的机器人。
        #   对面那句 @ / 引用了谁的都不算（11-08 复查）：@ / 引用的是他 —— 那是别人在叫他，被叫起来的机器人也这样一来一回；
        #   @ / 引用的是第三个人 —— 对面在跟别人说话。他自己那句只能 @ / 引用对面（或者谁也不 @）
        xp, px = defaultdict(int), defaultdict(int)
        for a, b in zip(seq, seq[1:]):
            if b[0] - a[0] > TALK_GAP or a[3] or b[3] or a[1] == b[1]:
                continue
            if b[1] == uid:
                mine, theirs = b, a
            elif a[1] == uid:
                mine, theirs = a, b
            else:
                continue
            p = theirs[1]
            if p == SELF or isb(p) or _addr(theirs) or (_addr(mine) - {p}):
                continue
            if mine is b:
                xp[p] += 1
            else:
                px[p] += 1
        n, who = max(((min(xp[p], px[p]), p) for p in xp), default=(0, ""))
        if n >= TALK_MIN:
            out.append(("talk", f"自己跟 {who} 来回聊了 {n} 轮（没人 @ / 引用着他、都没找你的机器人）"))
        # ③ 冷场很久以后自己开话题（@ 你的机器人开的也算：真人才会冷场以后来找它聊）。
        #   要冷场 20 分钟以上、最近一天 3 次以上：带「冷场试探」的机器人碰不上（见 INIT_COLD 那里）
        s24 = self.seq(gk, now, max(win, INIT_WIN))
        k = sum(1 for a, b in zip(s24, s24[1:]) if b[1] == uid and b[0] - a[0] >= INIT_COLD)
        if k >= INIT_MIN:
            out.append(("init", f"冷场 {INIT_COLD // 60} 分钟以上以后自己开过 {k} 次话题（最近一天）"))
        return out


def _addr(x):
    """这一句 @ 了谁、引用了谁的话"""
    return set(x[2]) | set(x[5] if len(x) > 5 else ())
