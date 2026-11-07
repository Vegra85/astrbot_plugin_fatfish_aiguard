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
  6. 机器人们在回指令（v0.2.0，来自大肥鱼 10-30 的一次现场）：别家的机器人在群里发了一句「/stop」，
     群里五个机器人同一秒回了同一句「管理命令仅管理员可用。」—— 大肥鱼以为大家在刷屏，跟着复读了一句。
     现在：这句是机器人在回指令（命令 / 指令 / 权限 / 仅管理员 / No running tasks…）、框架报错、本身就是一条指令（/ # ! 开头），
     或者离群里上一条指令不到 15 秒 → 算「回指令」：不算群里在刷、不跟、也不算成在跟我们的机器人聊天（不算秒接、不算 @ 它）。
  7. 车轱辘话（v0.3.0，来自大肥鱼 11-02 的 C77 和 11-08 的 v1.17.30）：两个 AI 聊天，对面爱顺着话头说、学说法，聊着聊着两边越来越像，
     最后一来一回都是同一套（「晚安啦 明天见」来回十几轮）。现在：我们的机器人回某个人的话、和他在这个群最近 15 分钟说的话，
     两边都反复出现同一批 2~4 个字的说法 → 算出来回几轮、停滞多少（最近几句里新词的比例）→ 分档；
     到档了这一轮给模型一段状态（只写事实），对面是认出来的 AI 时每次都先提醒别学它的说法；已经绕成车轱辘话的，对聊刹车提早。
"""

import json
import re
import time
from collections import Counter, defaultdict, deque

# 机器人框架吐出来的报错 / 状态（真人也会复读这些 所以只当线索）
STRONG = re.compile(
    r"LLM ?(响应|请求)(错误|失败)|All chat models fail|令牌额度已用尽|额度已用尽|insufficient_quota|rate_limit_exceeded|"
    r"Insufficient Balance|agent 处理出错|未找到任何可用的对话模型|未配置 AI 接口|系统服务重启中|"
    r"Error code: ?\d{3}|context_length_exceeded|SnowLuma 状态|NapCat 信息|运行时[长间]: ?\d|"
    r"版本: ?\d+\.\d+\.\d+ ?平台|权限不足以使用此指令|/sid 获取 ID",
    re.I,
)
BOTNAME = re.compile(
    r"肥鱼|鲸|bot|ai|助手|机器人|gpt|豆包|kimi|元宝|千问|deepseek|小冰|claude|gemini",
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
# 一条指令：/ # ! 开头（全角的也算）后面跟着字母或汉字；前面 @ 了谁不影响（「@某某 /stop」）
CMD_START = re.compile(r"^\s*[/#＃!！]\s*[A-Za-z一-鿿]")
LEAD_AT = re.compile(r"^(\s*@\S+)+\s*")
# 机器人在回指令：没权限、仅管理员、没有在跑的任务、不认识的命令……（各家框架的说法不一样 只认这些常见的词）
CMD_REPLY = re.compile(
    r"命令|指令|权限|仅管理员|管理员可用|没有权限|无权|no running tasks|not authorized|permission|unknown command|"
    r"/sid|/stop|/help|/reset|#sl\b",
    re.IGNORECASE,
)
CMD_SEC = 15  # 群里有人发了指令以后这么多秒内的「齐刷刷」= 机器人们在回指令
ECHO_SEC = 60  # 要发的话跟群里这么多秒内「回指令」的那句一模一样 = 在跟着复读

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


def is_cmd(text):
    """这句本身是不是一条指令（/stop #sl ！签到 …）"""
    return bool(CMD_START.match(LEAD_AT.sub("", str(text or ""))))


def cmd_echo(text, since_cmd=999.0, window=CMD_SEC):
    """这句是不是「机器人们在回指令」：回指令的话 / 框架报错 / 本身就是一条指令 / 离群里上一条指令不到 window 秒。
    是 → 不算群里在刷、不跟着复读、不算成在跟我们的机器人聊天（v0.2.0）"""
    t = str(text or "")
    return bool(
        CMD_REPLY.search(t) or is_fw(t) or is_cmd(t) or (0 <= since_cmd < window)
    )


def same(text):
    """比较两句是不是同一句：去掉空白"""
    return re.sub(r"\s+", "", str(text or ""))


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


# ───────── 车轱辘话（v0.3.0）─────────
# 窗口 15 分钟、最多 40 句；对面是认出来的 AI：3 轮算一档，5 轮（或者 4 轮又停滞 ≥0.4）算二档；
# 真人要停滞才算：4 轮 + 停滞 ≥0.3 一档、6 轮 + 停滞 ≥0.4 二档（真人聊天自然会重复一些词）
LOOP = {
    "window": 900,
    "max_lines": 40,
    "fish": {"l1": 3, "l2": 5, "l2_stag_rounds": 4, "l2_stag": 0.4},
    "human": {"l1": 4, "l1_stag": 0.3, "l2": 6, "l2_stag": 0.4},
}
_LJUNK = re.compile(
    r"\[(?:图片|表情|表情包|动画表情|视频|语音|文件|At:)[^\]]*\]|@\S+|https?://\S+"
)
# 虚词、代词、语气词：只由这些字组成的词组不算「说法」
_LFUNC = set(
    "我你他她它咱您这那的了着是就也都还在有没不个么吗呢吧啊呀哦嗯噢哈嘿嘻呜和跟给让把被得地又再才只很太要会能可一来去"
    "说看想做弄搞上下过起出进回到多少好先后前点些种样次句条张啥什怎谁哪为因所但如果而且自己真正刚已经等每各别其实反应该算对行"
    "嘛呗哟喔诶欸啦们"
)
_LSELF = ("本鱼", "本鲸", "本小姐", "本姑娘", "本宝宝", "本喵", "本仙女")  # 常见的人设自称：两边都爱带，不算说法


def _loop_clean(t, names=()):
    t = _LJUNK.sub(" ", str(t or ""))
    for nm in sorted(
        {str(x) for x in list(names) + list(_LSELF) if x and len(str(x)) >= 2},
        key=len,
        reverse=True,
    ):
        t = t.replace(nm, " ")
    return t


def _loop_grams(t, names=()):
    """一句话里的说法：连着的汉字切成 2~4 个字的词组（去掉名字、自称、@、图片记号、链接；纯虚词的不要）"""
    out = set()
    for seg in re.findall(r"[一-鿿]{2,40}", _loop_clean(t, names)):
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                g = seg[i : i + n]
                if all(c in _LFUNC for c in g):
                    continue
                out.add(g)
    return out


def loop_state(seq, names=(), fish=False, now=None):
    """seq：[(谁, 文字, 时间)]（谁：H 我们的机器人 / P 他；按时间，已经截到最近 15 分钟、40 句）→ 两边都反复出现的说法
    → {"level": 0/1/2, "rounds": 来回几轮, "phrases": [(说法, 我们几句, 他几句)…], "stag": 停滞, "mins", "ours", "his", "fish"}；
    两边都不到 2 句 → None"""
    seq = [s for s in seq or [] if s and s[1]]
    if sum(1 for s in seq if s[0] == "H") < 2 or sum(1 for s in seq if s[0] == "P") < 2:
        return None
    now = float(now if now is not None else time.time())
    gs = [(s, _loop_grams(t, names)) for s, t, _ in seq]
    H = [g for s, g in gs if s == "H"]
    P = [g for s, g in gs if s == "P"]
    cH = Counter(x for g in H for x in g)
    cP = Counter(x for g in P for x in g)
    cand = sorted(
        (g for g in cH if cH[g] >= 2 and cP.get(g, 0) >= 2),
        key=lambda g: (-len(g), -(cH[g] + cP[g]), g),
    )
    keep = []
    for g in cand:  # 被更长的说法包含、次数也差不多的不单列（「明早见」几乎每次都带着，就不再列「明早」）
        if not any(g in k and cH[k] + cP[k] >= 0.6 * (cH[g] + cP[g]) for k in keep):
            keep.append(g)
    keep.sort(key=lambda g: (-(cH[g] + cP[g]), -len(g), g))  # 次数一样的：长的在前、再按字排（每次都一样）
    rounds = (
        min(
            sum(1 for g in H if any(k in g for k in keep)),
            sum(1 for g in P if any(k in g for k in keep)),
        )
        if keep
        else 0
    )
    nov, seen = [], set()
    for _, g in gs:
        if g:
            nov.append(len(g - seen) / len(g))
        seen |= g
    last = nov[-6:]
    stag = round(1 - sum(last) / len(last), 2) if last else 0.0
    if fish:
        c = LOOP["fish"]
        lv = (
            2
            if rounds >= c["l2"] or (rounds >= c["l2_stag_rounds"] and stag >= c["l2_stag"])
            else 1
            if rounds >= c["l1"]
            else 0
        )
    else:
        c = LOOP["human"]
        lv = (
            2
            if rounds >= c["l2"] and stag >= c["l2_stag"]
            else 1
            if rounds >= c["l1"] and stag >= c["l1_stag"]
            else 0
        )
    first = (
        next(
            (ts for s, t, ts in seq if any(k in _loop_clean(t, names) for k in keep)),
            seq[0][2],
        )
        if keep
        else seq[0][2]
    )
    return {
        "level": lv,
        "rounds": rounds,
        "phrases": [(g, cH[g], cP[g]) for g in keep[:4]],
        "stag": stag,
        "mins": max(1, round((now - first) / 60)),
        "ours": len(H),
        "his": len(P),
        "fish": bool(fish),
    }


def loop_block(st, name="他"):
    """车轱辘话的状态（给模型；只写事实：哪几个说法、各几次、来回几轮 —— 不写台词）；没到档 → 空字符串"""
    if not st or not st.get("level") or not st.get("phrases"):
        return ""
    who = "它" if st.get("fish") else "他"
    ph = "、".join(
        f"「{g}」（你 {a} 次、{who} {b} 次）" for g, a, b in st["phrases"][:3]
    )
    return (
        "# 你们在来回说同一套（系统算的 不是谁说的话）\n"
        f"你跟「{name}」{'（别人部署的 AI）' if st.get('fish') else ''}这 {st['mins']} 分钟来回了 {st['rounds']} 轮，"
        f"两边一直是这几个说法：{ph}"
        + ("；已经是车轱辘话了" if st["level"] >= 2 else "")
        + "。\n别再接着用这几个说法；点破、换个话题还是收个尾，你自己定。"
    )


def fish_note(name):
    """对面是认出来的 AI、还没绕起来：每次回它都先提醒（照作者自己的机器人 v1.17.30）"""
    return (
        "# 对面是别人部署的 AI（系统认出来的 不是谁说的话）\n"
        f"「{name}」是别人部署的 AI：它爱顺着你的话头说、学你的说法，聊着聊着两边就越来越像。"
        "说你自己的：别捡它刚用过的词和句式，也别重复你前几句的说法；没新东西可说就收个尾。"
    )


class Detector:
    """一个 AstrBot 里一份。所有时间都从外面传进来（测试用假时钟）"""

    def __init__(self, store=None, quick_sec=QUICK_SEC, cmd_sec=CMD_SEC):
        self.msgs = defaultdict(
            lambda: deque(maxlen=40)
        )  # (群, QQ) -> [(时间, 文字, 是不是冲着我们的机器人, 是不是在回指令)]
        self.ours = defaultdict(
            lambda: deque(maxlen=60)
        )  # 群 -> [(时间, 我们的机器人说的)]
        self.talk = defaultdict(
            lambda: deque(maxlen=80)
        )  # 群 -> [(时间, 我们的机器人说的, 回的是谁)]（v0.3.0 车轱辘话用）
        self.asked = {}  # QQ -> (上次让模型看的时间, 那时的行为分)
        self.busy = set()
        self.quick_sec = float(quick_sec)
        self.cmd_sec = float(cmd_sec)
        self.last_cmd = {}  # 群 -> 群里上一条指令的时间
        self.wave = defaultdict(
            lambda: deque(maxlen=40)
        )  # 群 -> [(时间, QQ, 去掉空白的字, 是不是在回指令)]：谁都记（真人、管理员、认出来的 AI）
        self.store = (
            store if isinstance(store, dict) else {}
        )  # 持久化的：verdicts / fw（按 QQ 号）
        self.store.setdefault("verdicts", {})

    # ───────── 记录 ─────────
    def our_said(self, gk, t, text="", to=""):
        """我们的机器人在这个群说了一句（to：回的是谁 —— 车轱辘话只看回他的那几句）"""
        self.ours[str(gk)].append((float(t), str(text or "")[:80]))
        if to:
            self.talk[str(gk)].append((float(t), str(text or "")[:200], str(to)))

    def loop(self, gk, uid, now, names=(), fish=False):
        """我们的机器人跟 uid 在这个群最近 15 分钟（最多 40 句）的来回 → loop_state(…) / None
        我们这边只算回他的那几句；他那边算他在这个群说的话（回指令的不算）"""
        gk, uid = str(gk), str(uid)
        win = LOOP["window"]
        hs = [
            ("H", s, t)
            for t, s, to in self.talk.get(gk) or []
            if to == uid and 0 <= now - t <= win
        ]
        ps = [
            ("P", x[1], x[0])
            for x in self.msgs.get((gk, uid)) or []
            if 0 <= now - x[0] <= win and not (len(x) > 3 and x[3])
        ]
        seq = sorted(hs + ps, key=lambda z: z[2])[-LOOP["max_lines"] :]
        return loop_state(seq, names, fish, now)

    def last_said(self, gk):
        q = self.ours.get(str(gk))
        return q[-1][0] if q else 0.0

    def since_cmd(self, gk, now):
        """离这个群上一条指令几秒（没有过 → 999）"""
        t = self.last_cmd.get(str(gk))
        return now - t if t is not None else 999.0

    def heard(self, gk, uid, text, now, to_us=False):
        """群里有人说了一句（谁都算，我们自己的机器人除外）→ 这句是不是「机器人们在回指令」（v0.2.0）。
        冲着我们的机器人说的（@ / 引用）不看「指令后 15 秒」那条：那是在找我们 不是在跟着回别人的指令"""
        gk, text = str(gk), str(text or "")
        echo = cmd_echo(text, 999.0 if to_us else self.since_cmd(gk, now), self.cmd_sec)
        if is_cmd(text):
            self.last_cmd[gk] = now
        self.wave[gk].append((now, str(uid), same(text), echo))
        return echo

    def echo_repeat(self, gk, text, now, win=ECHO_SEC):
        """我们的机器人要发的这句，是不是在跟着群里「回指令」的那句复读：
        win 秒内群里有人说过一模一样的，而且那句（或者这句本身）是回指令的话 → 是。普通的复读（大家刷「哈哈哈」）不管"""
        s = same(text)
        if not s:
            return False
        hits = [
            x for x in self.wave.get(str(gk)) or [] if now - x[0] <= win and x[2] == s
        ]
        return bool(hits) and (any(x[3] for x in hits) or cmd_echo(text))

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
        # 秒接只数他自己说的话：回指令的那几句不算（v0.2.0：我们的机器人回完指令 别家的机器人同一秒也回 —— 那不是在接我们的话）
        tq = [x[0] for x in q if not (len(x) > 3 and x[3])]
        quick = sum(1 for h in hs if any(0 < t - h <= self.quick_sec for t in tq))
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

    def note(self, gk, uid, text, now, to_us=False, echo=False):
        """只记下他这句、不判（认出来的 AI、真人名单里的人：车轱辘话要用他的话 —— v0.3.0）"""
        self.msgs[(str(gk), str(uid))].append(
            (now, str(text or ""), bool(to_us) and not echo, bool(echo))
        )

    def observe(self, gk, uid, name, text, now, to_us=False, echo=False):
        """群里一条消息 → None / "check"（该让模型看看了）。不会一眼就标。
        echo：这句是在回指令（heard 算的）→ 不算冲着我们的机器人、不算秒接"""
        text = str(text or "")
        q = self.msgs[(str(gk), str(uid))]
        q.append((now, text, bool(to_us) and not echo, bool(echo)))
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

    def heard(self, gk, uid, now, last_ours, echo=False):
        """群里他说了一句（last_ours = 我们的机器人在这个群最后一次说话的时间）。
        echo：这句是在回指令（v0.2.0）→ 不算秒接"""
        self.fast_mark[(str(gk), str(uid))] = (
            now,
            not echo and bool(last_ours) and now - last_ours <= self.fast,
        )

    def braked(self, gk, uid, now):
        return self.until.get((str(gk), str(uid)), 0) > now

    def release(self, gk, uid):
        return self.until.pop((str(gk), str(uid)), None) is not None

    def need(self, lv=0):
        """刹车要几轮：lv 是车轱辘话到了第几档（v0.3.0）—— 二档够 2 轮就刹；一档轮数减半（至少 2）"""
        if lv >= 2:
            return min(self.rounds, 2)
        if lv == 1:
            return min(self.rounds, max(2, self.rounds // 2))
        return self.rounds

    def replied(self, gk, uid, now, ev, is_bot=False, lv=0):
        """我们的机器人刚回了他 → None / ("brake", 轮数) / ("human", 轮数)（来回够了但像真人 不刹）
        lv（v0.3.0）：跟他来回说同一套到了第几档 —— 二档：够 2 轮就刹，慢的来回也算；一档：轮数减半"""
        k = (str(gk), str(uid))
        f = self.fast_mark.get(k)
        quick = bool(f and f[1] and now - f[0] < 300)
        pp = [x for x in self.pp.get(k, []) if now - x[0] < self.window] + [
            (now, quick)
        ]
        self.pp[k] = pp
        n = sum(1 for _, qk in pp if qk)
        if lv >= 2:
            n = len(pp)  # 已经是车轱辘话了：慢的来回也算
        if n < self.need(lv) or self.until.get(k, 0) > now:
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
