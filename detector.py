# -*- coding: utf-8 -*-
"""
认别人家的 AI + 对聊刹车：判断逻辑（不依赖 AstrBot，单独能测）

思路（来自「大肥鱼」QQ 机器人里跑了两天的版本，10-27 修过一轮误判）：
  1. 群里每条消息都记一下（每个号最近 40 条）。看着像机器人的号（名字像 bot、成段工整的句子、几秒连发、
     自称「本鱼 / 本小姐」、括号动作、漏出来的思考过程 / 格式标签、框架报错……）攒够几条正常说的话以后，
     把它最近的话、跟我们机器人的来回、算好的「行为分」一起交给模型判断：是不是别人部署的 AI。
  2. 不会一眼就标：群友经常一起复读机器人的报错、模仿状态输出；有的真人号上挂着机器人框架（「混用号」）—— 这些都交给模型看完再说。
  3. 看过一次以后：证据一下子多了（行为分比上次多 8）2 分钟后再看；多 5 分 10 分钟后；没怎么多一天一次。
  4. 模型说是 AI、把握 ≥ 75 → 标；把握 ≥ 65 而且行为分 ≥ 8 → 也标；混用号不标。
  5. 对聊刹车：我们的机器人回了某个人以后，他要是在机器人说完 20 秒内就接上（大模型生成要 10~20 秒）算一轮「秒接」；
     10 分钟里够 8 轮、而且行为像机器人（连发 / 自称 / 框架输出）→ 先停 10 分钟不回他，同时叫模型马上再看一眼。
"""

import json
import re
import time
from collections import defaultdict, deque

# 机器人框架吐出来的报错 / 状态（真人也会复读这些 所以只当线索）
STRONG = re.compile(
    r"LLM ?(响应|请求)(错误|失败)|All chat models fail|令牌额度已用尽|额度已用尽|insufficient_quota|rate_limit_exceeded|"
    r"Insufficient Balance|agent 处理出错|未找到任何可用的对话模型|未配置 AI 接口|系统服务重启中|"
    r"Error code: ?\d{3}|context_length_exceeded|SnowLuma 状态|NapCat 信息|运行时[长间]: ?\d|"
    r"版本: ?\d+\.\d+\.\d+ ?平台|权限不足以使用此指令|/sid 获取 ID",
    re.I,
)
BOTNAME = re.compile(
    r"肥鱼|鲸|鱼丸|bot|ai|助手|机器人|gpt|豆包|kimi|元宝|千问|deepseek|小冰|claude|gemini",
    re.I,
)
SELFISH = re.compile(
    r"本鲸|本鱼|本(大)?小姐|本姑娘|本宝宝|本喵|本仙女|本鲸鲸|只能打字|发不了语音|看不了图|没法出声|"
    r"我(是|只是|就是)(一个|一条|一只)?.{0,4}(ai|人工智能|机器人|大模型|鲸鱼娘)|"
    r"作为(一个)?ai|住在(电脑|这框|屏幕|服务器|机房)里|被主人@|主人@了",
    re.I,
)
BRACKET = re.compile(
    r"[（(](?!回复了)[^（）()]{4,30}[）)]"
)  # 括号动作「（尾巴一摆，缩回屏幕后面去了）」
ATNL = re.compile(
    r"^@[^\s@]{1,30}[ 　]*\n"
)  # 「@某人」后面直接换行再说话（bot 框架的回复格式）
DBL = re.compile(r"[。！？!?，,]\s{2,}\S")
THINK = re.compile(
    r"^\s*[\"“]?(用户|对方)(在|又|刚|正在)?(@我|艾特我|问我|说|发了|让我|想让我)|我作为.{0,16}(需要|应该|得)|"
    r"需要(自然|简短|礼貌|简单)?地?回(应|复)|^\s*(简短回复|直接回|不用查资料|不用(调用)?工具|这是(在)?(闲聊|聊)|"
    r"话题是|接个梗就行|我(先)?(想想|分析一下)|思考[:：]|回复思路)|(根据|按照?)(人设|设定|系统提示|system ?prompt)",
    re.I | re.M,
)
# 漏出来的格式标签：成对的「<h>这不是本小姐吗</h>」、或者开头就是收尾标签「</think>好的…」；真人问「<think> 标签怎么去掉」不算
MARKUP = re.compile(
    r"<(h|think|thinking|reply|msg|message|response|output|answer|sys|system|assistant|content|text)>[^<\s][^<]{0,119}</\1>"
    r"|^\s*</(h|think|thinking|reply|response|output|answer|assistant)>",
    re.I | re.M,
)
MEDIA = re.compile(r"\[(表情|图片|表情包|动画表情|视频|At:[^\]]*)[^\]]*\]")

BURST_GAP = 6  # 思考过程后面隔不到 6 秒连着发的 也算框架发的
QUICK_SEC = 20  # 我们的机器人说完这么多秒内就接上 = 秒接
JUMP_SEC = 120  # 证据一下子多了 8 分以上：2 分钟后就能再看
RECHECK_SEC = 600  # 多了 5 分：10 分钟后
DAILY_SEC = 24 * 3600  # 没怎么多：一天一次

JUDGE = (
    "你在帮一个 QQ 群聊机器人（下面叫「我们的机器人」）分辨：群里某个账号是不是「别人部署的 AI 聊天机器人」（自动回复的大模型 bot），还是真人。\n"
    "很多 bot 是照着「真人群友」调教的：短句、口语、不打标点、会玩梗、会撒娇 —— 光看句子像真人不算数。"
    "更可靠的是行为（下面都替你算好了）：我们的机器人一说完 10~20 秒（大模型生成的时间）就接上、一接就是两三条、句句冲着我们的机器人、"
    "一来一回停不下来、自称「本鱼 / 本鲸 / 本小姐 / 本喵」这种固定的人设自称、说自己住在电脑里、写括号动作（「（尾巴一摆，缩回去了）」）、"
    "在群里吐过 bot 框架的状态（SnowLuma 状态、NapCat 信息）或者指令报错（「权限不足以使用此指令」）、漏出格式标签（「<h>…</h>」）。\n"
    "AI 的样子还有：句子完整通顺、标点规范、句号结尾、两句之间有双空格；会说自己发不了图 / 出不了声 / 只能打字；会报错（LLM 响应错误、额度用尽）。\n"
    "真人的样子：回得有快有慢、话题跳、错别字、发指令（/xxx #xxx）、使唤机器人干活、骂人挑衅、跟大家聊而不是只围着我们的机器人转。"
    "真人也会跟机器人聊得很勤、回得快、@ 它 引用它 —— 光凭这个不算；要看是不是跟机器人的样子一起出现，还有他说的话本身像不像人。\n"
    "注意：群友经常一起复读、模仿机器人的报错和状态输出（一堆人刷「LLM 请求失败…」）—— 那是真人在玩梗。\n"
    "有的真人号上挂着机器人框架：平时是本人在聊（口语、@别人问事、聊自己的电脑和 token），偶尔冒出一段机器人的输出"
    "（状态、报错、漏出来的思考过程、一段很工整的回复）—— 这叫「混用号」 算真人：bot=false、mixed=true。混用号的本人不会跟机器人秒接地一来一回。\n"
    "写成「[机器人框架的输出…]」的 是系统已经认出来的框架输出：只当线索 别当成这个号自己说的话。拿不准就给低把握。\n"
    '只回一行 JSON：{"bot": true 或 false, "mixed": true 或 false, "confidence": 0-100, "why": "十个字以内"}'
)


def words(x):
    """去掉表情 / 图片记号后 这条真正说的字"""
    return MEDIA.sub("", str(x or "")).strip()


def is_fw(text):
    return bool(STRONG.search(text) or MARKUP.search(text))


def flags(q):
    """每条是不是机器人框架的输出：报错 / 状态 / 标签、漏出来的思考过程，还有思考过程后面几秒内接着发的"""
    think = [bool(THINK.search(x[1])) for x in q]
    out = [bool(t or is_fw(x[1])) for t, x in zip(think, q)]
    for i, f in enumerate(think):
        j = i + 1
        while f and j < len(q) and q[j][0] - q[j - 1][0] <= BURST_GAP:
            out[j] = True
            j += 1
    return out


class Detector:
    """一个 AstrBot 里一份。所有时间都从外面传进来（测试用假时钟）"""

    def __init__(self, store=None, quick_sec=QUICK_SEC):
        self.msgs = defaultdict(
            lambda: deque(maxlen=40)
        )  # (群, QQ) -> [(时间, 文字, 是不是冲着我们的机器人)]
        self.ours = defaultdict(
            lambda: deque(maxlen=60)
        )  # 群 -> [(时间, 我们的机器人说的)]
        self.asked = {}  # QQ -> (上次让模型看的时间, 那时的行为分)
        self.busy = set()
        self.quick_sec = float(quick_sec)
        self.store = (
            store if isinstance(store, dict) else {}
        )  # 持久化的：verdicts / fw（按 QQ 号）
        self.store.setdefault("verdicts", {})

    # ───────── 记录 ─────────
    def our_said(self, gk, t, text=""):
        self.ours[str(gk)].append((float(t), str(text or "")[:80]))

    def last_said(self, gk):
        q = self.ours.get(str(gk))
        return q[-1][0] if q else 0.0

    def _note_fw(self, uid, gk, text, now):
        m = STRONG.search(text) or MARKUP.search(text)
        if not m:
            return
        r = self.store["verdicts"].setdefault(str(uid), {})
        r["fw"] = int(r.get("fw") or 0) + 1
        r["fw_eg"], r["fw_g"], r["fw_t"] = m.group(0)[:20], str(gk), now

    def evidence(self, gk, uid, now, win=2 * 3600):
        """这个号最近 win 秒在这个群的行为"""
        q = [x for x in self.msgs.get((str(gk), str(uid))) or [] if now - x[0] < win]
        fw = flags(q)
        plain = [x for x, f in zip(q, fw) if not f]
        ts = [x[0] for x in q]
        hs = [t for t, _ in self.ours.get(str(gk)) or [] if now - t < win]
        quick = sum(1 for h in hs if any(0 < t - h <= self.quick_sec for t in ts))
        st = self.store["verdicts"].get(str(uid)) or {}
        # 连发：他自己两条之间不到 3.5 秒、而且中间我们的机器人没说话（一接就是两三条）；
        # 跟机器人你一句我一句聊得快的真人不算（机器人回得快的时候 他的两句也会挨得很近）
        burst = sum(
            1
            for a, b in zip(ts, ts[1:])
            if b - a <= 3.5 and not any(a < h < b for h in hs)
        )
        ev = {
            "quick": quick,
            "ours": len(hs),
            "to_us": sum(1 for x in q if len(x) > 2 and x[2]),
            "burst": burst,
            "selfref": sum(1 for x in plain if SELFISH.search(x[1])),
            "atnl": sum(1 for x in q if ATNL.search(x[1])),
            "bracket": sum(1 for x in plain if BRACKET.search(x[1])),
            "fw": int(st.get("fw") or 0),
            "fw_eg": str(st.get("fw_eg") or ""),
        }
        # 真人也会跟机器人聊得勤、回得快、@它 —— 这两样只算一点；连发、自称、括号动作、框架输出算得重
        ev["score"] = (
            min(ev["quick"], 10) // 2
            + min(ev["to_us"], 10) // 4
            + min(ev["burst"], 10) // 2
            + 2 * min(ev["selfref"], 5)
            + 2 * min(ev["bracket"], 5)
            + 3 * min(ev["fw"], 3)
            + 3 * min(ev["atnl"], 3)
        )
        return ev

    def observe(self, gk, uid, name, text, now, to_us=False):
        """群里一条消息 → None / "check"（该让模型看看了）。不会一眼就标"""
        text = str(text or "")
        q = self.msgs[(str(gk), str(uid))]
        q.append((now, text, bool(to_us)))
        if is_fw(text):
            self._note_fw(uid, gk, text, now)
        items = [x for x in q if now - x[0] < 2 * 3600 and x[1]]
        fw = flags(items)
        strong = sum(fw)
        recent = [(x[0], words(x[1])) for x, f in zip(items, fw) if not f]
        recent = [(t, x) for t, x in recent if len(x) >= 2]
        if (
            len(recent) < 8 or len({x for _, x in recent}) < 5
        ):  # 正常说的话不够 / 一直复读同一句：先不看
            return None
        st = self.store["verdicts"].get(str(uid)) or {}
        ev = self.evidence(gk, uid, now)
        a_t, a_s = self.asked.get(str(uid)) or (0, 0)
        last = max(a_t, float(st.get("t") or 0))
        if last:
            since = now - last
            base = max(
                a_s if a_t >= float(st.get("t") or 0) else 0, int(st.get("score") or 0)
            )
            grow = ev["score"] - base if ev["score"] >= 8 else 0
            if not (
                (since >= JUMP_SEC and grow >= 8)
                or (since >= RECHECK_SEC and grow >= 5)
                or since >= DAILY_SEC
            ):
                return None
        lens = [len(x) for _, x in recent]
        avg = sum(lens) / len(lens)
        period = sum(1 for _, x in recent if x.rstrip().endswith("。")) / len(recent)
        dbl = sum(1 for _, x in recent if DBL.search(x))
        burst = sum(1 for (a, _), (b, _) in zip(recent, recent[1:]) if b - a < 4) / max(
            1, len(recent) - 1
        )
        hint = (
            strong >= 1
            or bool(BOTNAME.search(name or ""))
            or dbl >= 2
            or ev["selfref"] >= 1
            or (avg >= 22 and period >= 0.3)
            or (burst >= 0.3 and avg >= 15)
            or (ev["quick"] >= 3 and ev["burst"] >= 5)
            or ev["fw"] >= 1
            or ev["bracket"] >= 2
            or ev["atnl"] >= 2
        )
        if not hint:
            return None
        self.asked[str(uid)] = (now, ev["score"])
        return "check"

    # ───────── 交给模型 ─────────
    def ev_text(self, ev):
        out = [
            f"我们的机器人最近 2 小时在这个群说了 {ev['ours']} 句；这个号在它说完 {int(self.quick_sec)} 秒内就接上 {ev['quick']} 次",
            f"@它 / 引用它 {ev['to_us']} 次",
            f"3 秒内连发 {ev['burst']} 次",
            f"自称本鱼 / 本小姐 / 住在电脑里这类 {ev['selfref']} 句",
            f"括号动作 {ev['bracket']} 句",
        ]
        if ev["fw"]:
            out.append(
                f"这个号（按 QQ 号算 不管哪个群）吐过 {ev['fw']} 次 bot 框架的状态 / 报错 / 格式标签（例：{ev['fw_eg']}）"
            )
        if ev["atnl"]:
            out.append(
                f"{ev['atnl']} 句是「@某人」后面直接换行再说话（bot 框架的回复格式 真人几乎不会这样）"
            )
        return "；".join(out)

    def judge_body(self, gk, uid, name, now):
        q = list(self.msgs[(str(gk), str(uid))])[-24:]
        if not q:
            return ""
        fw = flags(q)
        lines, prev = [], q[0][0]
        for x, f in zip(q, fw):
            t, s = x[0], x[1]
            if f:
                m = STRONG.search(s) or MARKUP.search(s)
                s = (
                    f"[机器人框架的输出：报错/状态/标签 {m.group(0)[:16]}]"
                    if m
                    else "[机器人框架的输出：漏出来的思考过程/自动回复]"
                )
            if lines and lines[-1][1] == s:
                lines[-1][2] += 1
            else:
                lines.append([int(t - prev), s, 1])
            prev = t
        lines = [
            f"（隔 {g} 秒）{x[:160]}"
            + (
                (
                    f"（连着 {n} 条）"
                    if x.startswith("[机器人框架")
                    else f"（同一句连发 {n} 次）"
                )
                if n > 1
                else ""
            )
            for g, x, n in lines
        ]
        body = f"账号名「{name}」最近的消息：\n" + "\n".join(lines)
        hs = [(t, s) for t, s in self.ours.get(str(gk)) or [] if t >= q[0][0] - 60]
        if hs:
            conv = sorted(
                [(t, "我们", s) for t, s in hs] + [(x[0], "他", x[1]) for x in q],
                key=lambda z: z[0],
            )[-16:]
            rows, last_us = [], None
            for t, who, s in conv:
                if who == "我们":
                    rows.append(f"[我们的机器人] {s[:70]}")
                    last_us = t
                else:
                    gap = (
                        f" · 我们的机器人说完 {int(t - last_us)} 秒"
                        if last_us is not None and t - last_us < 120
                        else ""
                    )
                    rows.append(f"[他{gap}] {s[:70]}")
            body += "\n\n我们的机器人和这个号最近的来回（按时间）：\n" + "\n".join(rows)
        body += "\n\n算好的行为：" + self.ev_text(self.evidence(gk, uid, now))
        return body

    @staticmethod
    def parse(out):
        """模型的回答 → (是不是 AI, 把握, 理由, 混用号)；看不懂 → (False, 0, "", False)"""
        try:
            m = re.search(r"\{.*\}", out or "", re.S)
            j = json.loads(m.group(0)) if m else {}
        except Exception:
            j = {}
        try:
            conf = int(float(j.get("confidence") or 0))
        except (TypeError, ValueError):
            conf = 0
        return (
            bool(j.get("bot")),
            max(0, min(100, conf)),
            str(j.get("why") or "")[:30],
            bool(j.get("mixed")),
        )

    def decide(self, uid, name, gk, verdict, now, threshold=75, low=65, low_score=8):
        """模型判完 → ("mark" / "mixed" / "unsure" / "human", 行为, 说明)；判断结果记进 store"""
        is_bot, conf, why, mixed = verdict
        ev = self.evidence(gk, uid, now)
        fp = ev["atnl"] >= 3 and not mixed  # 框架的回复格式出现好几次：模型说像真人也标
        r = self.store["verdicts"].setdefault(str(uid), {})
        r.update(
            t=now,
            name=name,
            bot=(is_bot or fp) and not mixed,
            conf=conf,
            why=why,
            score=int(ev["score"]),
        )
        if mixed:
            r["mixed"] = True
        else:
            r.pop("mixed", None)
        if mixed:
            return "mixed", ev, f"像是真人号挂着机器人（{why} 把握 {conf}）不标"
        if is_bot and conf >= threshold:
            return "mark", ev, f"模型看了最近的话：{why}（把握 {conf}）"
        if is_bot and conf >= low and ev["score"] >= low_score:
            return (
                "mark",
                ev,
                f"模型看了最近的话：{why}（把握 {conf}）+ 行为分 {ev['score']}",
            )
        if fp:
            return (
                "mark",
                ev,
                f"bot 框架的回复格式（@人后直接换行）{ev['atnl']} 次；模型把握 {conf}",
            )
        if is_bot:
            return (
                "unsure",
                ev,
                (
                    f"模型说像 AI 但把握只有 {conf}（要 {threshold}；行为分 {ev['score']} 不到 {low_score} 就要 {threshold}）"
                    f"先不标 证据再多了会再看（{why}）"
                ),
            )
        return (
            "human",
            ev,
            f"模型看了说是真人（{why} 把握 {conf}；行为分 {ev['score']}）",
        )


class Brake:
    """对聊刹车：我们的机器人回了这个人 → 他那句要是机器人说完 fast 秒内接上的 算一轮；window 秒里够 rounds 轮 → 停 pause 秒"""

    def __init__(self, rounds=8, window=600, pause=600, fast=20):
        self.rounds, self.window, self.pause, self.fast = (
            int(rounds),
            float(window),
            float(pause),
            float(fast),
        )
        self.fast_mark = {}  # (群, QQ) -> (他这句的时间, 是不是秒接)
        self.pp = {}  # (群, QQ) -> [(时间, 是不是秒接)]
        self.until = {}  # (群, QQ) -> 刹到几点
        self.told = {}

    def heard(self, gk, uid, now, last_ours):
        """群里他说了一句（last_ours = 我们的机器人在这个群最后一次说话的时间）"""
        self.fast_mark[(str(gk), str(uid))] = (
            now,
            bool(last_ours) and now - last_ours <= self.fast,
        )

    def braked(self, gk, uid, now):
        return self.until.get((str(gk), str(uid)), 0) > now

    def release(self, gk, uid):
        return self.until.pop((str(gk), str(uid)), None) is not None

    def replied(self, gk, uid, now, ev, is_bot=False):
        """我们的机器人刚回了他 → None / ("brake", 轮数) / ("human", 轮数)（来回够了但像真人 不刹）"""
        k = (str(gk), str(uid))
        f = self.fast_mark.get(k)
        quick = bool(f and f[1] and now - f[0] < 300)
        pp = [x for x in self.pp.get(k, []) if now - x[0] < self.window] + [
            (now, quick)
        ]
        self.pp[k] = pp
        n = sum(1 for _, qk in pp if qk)
        if n < self.rounds or self.until.get(k, 0) > now:
            return None
        if (
            ev["burst"] < 8
            and ev["selfref"] + ev["bracket"] < 2
            and ev["atnl"] < 2
            and ev["fw"] < 1
            and not is_bot
        ):
            if now - self.told.get(k, 0) > self.window:
                self.told[k] = now
                return ("human", n)
            return None
        self.until[k] = now + self.pause
        self.pp[k] = []
        return ("brake", n)


def now():
    return time.time()
