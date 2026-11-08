# -*- coding: utf-8 -*-
"""
鱼力max · 认 AI + 对聊刹车（AstrBot 插件）

不用填名单：群里别人部署的 AI 聊天机器人，它自己认出来；认出来以后只有 @ / 引用你的机器人才回，10 分钟最多回 3 次，
不会被它拖进停不下来的对骂 / 互道晚安。还没认出来之前，对聊刹车兜底：来回太快太多、对方又像机器人 → 先停 10 分钟。
别跟着机器人们回指令（v0.2.0）：有人发了「/stop」、几个机器人同一秒回「仅管理员可用」—— 不当成群里在刷、不跟、不当成在找你的机器人聊天。
车轱辘话（v0.3.0）：你的机器人跟同一个人来回说同一套 → 这一轮给模型状态；对面是认出来的 AI → 先提醒别学它的说法；绕成车轱辘话了 → 刹车提早。
自报家门（v0.4.0）：群名片 / 昵称 / 签名里挂着「[bot]」的号直接当机器人（不攒、不调模型、这一句就算）；一键给自己挂上（默认关）。
不对称分（v0.4.0，影子模式）：群里说话的节奏（总在别人说完几秒内接、不起话头、接话快慢很固定）→ 一个分，先只记不判。
真人信号一票否决（v0.4.0）：主动 @ 过好几个人、跟别人来回、自己开过话题的号，光靠行为分不自动标，只记「观察」。
判断逻辑在 detector.py 和 core/（不依赖 AstrBot，可以单独测）。
"""

import asyncio
import copy
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp

try:  # 加给模型的那段状态（这一轮临时的，不进聊天记录）；老版本没有 → 不加
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None

from .angel import release_angel
from .core.rhythm import (
    ASYM_BONUS,
    ASYM_MIN,
    SELF as RH_SELF,
    Rhythm,
    asym_bonus,
    asym_text,
    separation,
)
from .core.selfreport import (
    DEFAULT_TAGS,
    SELF_TAG,
    SignCache,
    find_tag,
    put_tags,
    seen_update,
    sign_of,
    tag_list,
    tag_report,
    tag_rx,
    take_tags,
)
from .detector import (
    JUDGE,
    KIND_TEXT,
    Brake,
    Detector,
    cmd_echo,
    fish_note,
    is_cmd,
    loop_block,
)

PLUGIN_NAME = "astrbot_plugin_fatfish_aiguard"
TAG = "[认AI]"
TAG_B = "[对聊刹车]"


def _num(v, d):
    try:
        return type(d)(v)
    except (TypeError, ValueError):
        return d


class FatfishAIGuard(Star):
    """认别人家的 AI + 对聊刹车。管理员指令：/认ai 列表 | 真人 <QQ> | 标记 <QQ> | 看看 <QQ> | 刹车 | 挂标记 | 摘标记"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = Path(os.getcwd()) / "data" / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.state = self._load()
        self.det = Detector(
            self.state.setdefault("detector", {}),
            quick_sec=self._c("brake_fast_sec", 20),
            cmd_sec=self._c("cmd_echo_sec", 15),
        )
        self.brake = Brake(
            self._c("brake_rounds", 8),
            self._c("brake_window_sec", 600),
            self._c("brake_pause_sec", 600),
            self._c("brake_fast_sec", 20),
        )
        self.events = deque(self.state.get("events") or [], maxlen=200)
        self.allowed = {}  # (群, QQ) -> [我们回这个机器人的时间]
        self.judged_today = [time.strftime("%Y-%m-%d"), 0]
        self._echo_log = {}  # (群, 哪种) -> 上次记日志的时间（回指令那一阵一秒好几条 日志别刷屏）
        self._loop_log = {}  # (群, QQ, 档) -> 上次记日志的时间（v0.3.0 车轱辘话）
        # v0.4.0：群里说话的时间线（不对称分、真人信号；只在内存里）、查签名的限速和缓存（存档里：重启不用全群重查）
        self.rh = Rhythm()
        self.signs = SignCache(self.state.setdefault("signs", {}))
        self._rx = (None, None)  # (写法列表, 正则)：配置没变就不重新编
        self._dirty = False
        self._closed = False
        self._tasks = set()
        self._saver = None
        self._register_web()

    # ───────── 配置 / 存档 ─────────
    def _c(self, key, default):
        return _num(self.config.get(key, default), default)

    def _guard(self):
        """别跟着机器人们回指令（v0.2.0）开着没有；老配置里没有这一项 = 开"""
        return bool(self._c("cmd_echo_guard", True))

    def _loop_on(self):
        """车轱辘话（v0.3.0）开着没有；老配置里没有这一项 = 开"""
        return bool(self._c("loop_enable", True))

    def _load(self):
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self, force=False):
        """→ 这次存到硬盘上了没有（force=False 只是记一下、等存档循环去存）"""
        self._dirty = True
        if not force:
            # v0.4.0：自报家门这一句就标上、不经过 _spawn —— 存档的循环也要能从这里起
            self._ensure_saver()
            return False
        try:
            self.state["events"] = list(self.events)[-100:]
            self.signs.prune(time.time())
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.state_file)
            self._dirty = False
            return True
        except Exception as e:
            logger.warning(f"{TAG} 存档失败：{e!r}")
            return False

    def _persist(self):
        """一键挂标记改每个群之前调：原名片得先落到硬盘上，存不下来就抛（那个群就不改）"""
        if not self._save(force=True):
            raise RuntimeError("存档失败")

    async def _save_loop(self):
        while True:
            await asyncio.sleep(15)
            if self._dirty:
                self._save(force=True)

    def _ensure_saver(self):
        if self._closed or (self._saver is not None and not self._saver.done()):
            return
        try:
            self._saver = asyncio.get_running_loop().create_task(self._save_loop())
        except RuntimeError:
            pass  # 还没有跑着的事件循环（插件刚加载）：下次再起

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        self._ensure_saver()

    def _event(self, kind, text, gk="", uid=""):
        e = {
            "t": int(time.time()),
            "kind": kind,
            "text": str(text)[:200],
            "g": str(gk),
            "uid": str(uid),
        }
        self.events.append(e)
        self._save()
        logger.info(f"{TAG_B if kind == 'brake' else TAG} {text}")

    async def terminate(self):
        self._closed = True
        for t in list(self._tasks):
            t.cancel()
        if self._saver:
            self._saver.cancel()
        self._save(force=True)

    # ───────── 名单 ─────────
    def _bots(self):
        return self.state.setdefault("bots", {})

    def _humans(self):
        return self.state.setdefault("humans", {})

    def _cfg_list(self, key):
        v = self.config.get(key) or []
        if isinstance(v, str):
            v = [x for x in v.replace("，", ",").replace(" ", ",").split(",")]
        return {str(x).strip() for x in v if str(x).strip()}

    def _admins(self):
        try:
            return {str(x) for x in (self.context.get_config().get("admins_id") or [])}
        except Exception:
            return set()

    def is_bot(self, uid):
        uid = str(uid)
        return uid in self._bots() or uid in self._cfg_list("always_bot")

    def is_human(self, uid):
        uid = str(uid)
        return (
            uid in self._humans()
            or uid in self._cfg_list("never_bot")
            or uid in self._admins()
        )

    def mark(self, uid, name, gk, why, auto=True, src="", tag=""):
        rec = {
            "name": name,
            "why": why,
            "t": int(time.time()),
            "g": gk,
            "auto": auto,
        }
        if src:  # v0.4.0 自报家门：从哪看到的标记（self_card / self_nick / self_sign）—— 以后看它摘没摘
            # 群名片是每个群一个：记在哪个群看到的；昵称、签名全 QQ 一个
            first = {"card": {str(gk): True}} if src == "self_card" else {src[5:]: True}
            rec.update(src=src, tag=tag, seen=first)
        self._bots()[str(uid)] = rec
        self._humans().pop(str(uid), None)
        self._event(
            "mark",
            f"{name}({uid}) 是别人家的 AI → 以后只有 @ / 引用才回（{why}）",
            gk,
            uid,
        )

    def unmark(self, uid, note="管理员说是真人"):
        uid = str(uid)
        old = self._bots().pop(uid, None)
        self._humans()[uid] = {
            "note": note,
            "t": int(time.time()),
            "name": (old or {}).get("name", ""),
        }
        for k in [k for k in self.brake.until if k[1] == uid]:
            self.brake.until.pop(k, None)
        self._event(
            "human",
            f"{(old or {}).get('name', '') or uid}({uid}) 记成真人（{note}）以后不再自动标",
            "",
            uid,
        )

    # ───────── 读消息 ─────────
    @staticmethod
    def _gk(event):
        return f"{event.get_platform_id()}:{event.get_group_id()}"

    @staticmethod
    def _raw(event, key):
        """QQ 原始事件里的一项（aiocqhttp 的 Event 是个 dict）；不是 QQ 平台 / 没有 → None"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        try:
            return raw.get(key) if hasattr(raw, "get") else getattr(raw, key, None)
        except Exception:
            return None

    @classmethod
    def _names(cls, event):
        """(群名片, QQ 昵称)：AstrBot 只给「有名片用名片、没有用昵称」的一个名字，自报家门要分开看两样"""
        s = cls._raw(event, "sender")
        if isinstance(s, dict):
            return str(s.get("card") or ""), str(s.get("nickname") or "")
        return "", str(event.get_sender_name() or "")

    @classmethod
    def _ats(cls, event):
        """这句 @ 了谁（不含你的机器人、全体成员）：真人信号「主动 @ 过好几个不同的人」要用（v0.4.0）"""
        me, out = str(event.get_self_id()), []
        segs = cls._raw(event, "message")
        if isinstance(segs, list) and segs and isinstance(segs[0], dict):
            qs = [
                str((s.get("data") or {}).get("qq") or "")
                for s in segs
                if isinstance(s, dict) and s.get("type") == "at"
            ]
        else:
            qs = [
                str(getattr(c, "qq", "") or "")
                for c in event.get_messages() or []
                if isinstance(c, Comp.At)
            ]
        for q in qs:
            if q and q not in (me, "all") and q not in out:
                out.append(q)
        return out

    @staticmethod
    def _refs(event):
        """这句引用了谁的话（不含你的机器人）：AstrBot 解析引用时查了被引用那句是谁发的（Reply.sender_id）。
        真人信号「自己找别人来回」要跳过别人 @ / 引用着他聊的那几对（11-08 复查）"""
        me, out = str(event.get_self_id()), []
        for c in event.get_messages() or []:
            if isinstance(c, Comp.Reply):
                q = str(getattr(c, "sender_id", "") or getattr(c, "qq", "") or "")
                if q and q not in ("0", me) and q not in out:
                    out.append(q)
        return out

    @classmethod
    def _text(cls, event):
        """这条消息的文字：优先用 QQ 原始消息段（保留换行 —— 「@人 换行 说话」是 bot 框架的格式）"""
        segs = cls._raw(event, "message")
        if isinstance(segs, list) and segs and isinstance(segs[0], dict):
            out = []
            for s in segs:
                t, d = s.get("type"), s.get("data") or {}
                if t == "text":
                    out.append(str(d.get("text") or ""))
                elif t == "at":
                    out.append(f"@{d.get('qq')}")
                elif t in ("image", "mface"):
                    out.append("[图片]")
                elif t == "face":
                    out.append("[表情]")
                elif t == "record":
                    out.append("[语音]")
            return "".join(out).strip()
        out = []
        for c in event.get_messages() or []:
            if isinstance(c, Comp.Plain):
                out.append(c.text)
            elif isinstance(c, Comp.At):
                out.append(f"@{c.qq}")
            elif isinstance(c, Comp.Image):
                out.append("[图片]")
        return ("".join(out) or event.message_str or "").strip()

    def _skip(self, event, uid):
        if not uid or str(uid) == str(event.get_self_id()):
            return True
        gs = self._cfg_list("groups")
        return bool(gs) and str(event.get_group_id()) not in gs

    # ───────── 自报家门（v0.4.0） ─────────
    def _tag_rx(self):
        tags = tuple(tag_list(self.config.get("self_report_tags", list(DEFAULT_TAGS))))
        if self._rx[0] != tags:
            self._rx = (tags, tag_rx(tags))
        return self._rx[1]

    def _self_report(self, event, gk, uid, name, now):
        """群名片 / 昵称里挂着标记（每条消息都带着，不用另外查）→ 直接当机器人：不攒、不调模型、这一句就算。
        已经是自报家门认出来的：看标记还在不在（摘了也不撤，只在面板上标出来）"""
        if not self._c("self_report", True):
            return
        rx = self._tag_rx()
        card, nick = self._names(event)
        ct, nt = find_tag(card, rx), find_tag(nick, rx)
        if uid in self._bots():
            self._tag_seen(uid, name, gk, now, card=ct, nick=nt)
            return
        # 管理员撤过的（记成真人）、AstrBot 管理员、配置里永远当真人的：标记挂着也不认 —— 管理员说了算
        if not (ct or nt) or self.is_bot(uid) or self.is_human(uid):
            return
        where, tag, src = ("群名片", ct, "self_card") if ct else ("昵称", nt, "self_nick")
        self.mark(uid, name, gk, f"自报家门（{where}）：挂着「{tag}」", src=src, tag=tag)

    def _tag_seen(self, uid, name, gk, now, **chan):
        """自报家门认出来的号：这次看到的群名片 / 昵称 / 签名里还有没有标记。
        都没了 → 记「标记已摘」，但不撤（来回挂挂摘摘的话 认 / 不认跟着来回切 更乱）；管理员要撤就 /认ai 真人"""
        b = self._bots().get(str(uid))
        if not b or not str(b.get("src") or "").startswith("self_"):
            return
        before = copy.deepcopy(b.get("seen") or {})
        what = seen_update(b, now, gk=gk, **chan)
        who = f"{b.get('name') or name}({uid})"
        if what == "back":
            self._event("tag", f"{who} 又挂上了自报家门的标记", gk, uid)
        elif what == "gone":
            self._event(
                "tag",
                f"{who} 把自报家门的标记摘了 → 照旧当它是别人家的 AI（不自动撤，免得来回切）；"
                f"其实是真人的话：/认ai 真人 {uid}",
                gk,
                uid,
            )
        elif b.get("seen") != before:
            self._save()

    def _maybe_sign(self, event, gk, uid, name, now):
        """要不要查他的 QQ 签名（调 NapCat 的 get_stranger_info）：每个号一天最多一次、全局限速；查是在后台查，不耽误这条消息。
        已经认出来的号不查（不用了）—— 只有签名认出来的，隔天再看一眼标记还在不在"""
        if not (self._c("self_report", True) and self._c("self_report_sign", True)):
            return
        if not uid.isdigit() or self.is_human(uid):
            return
        b = self._bots().get(uid)
        if (b is not None and b.get("src") != "self_sign") or (
            b is None and self.is_bot(uid)
        ):
            return
        bot = getattr(event, "bot", None)
        if not callable(getattr(bot, "call_action", None)):
            return
        if not self.signs.due(
            uid,
            now,
            self._c("sign_check_gap_sec", 30),
            self._c("sign_check_daily_max", 300),
        ):
            return
        self.signs.start(uid, now)
        self._save()
        self._spawn(self._sign_lookup(bot, gk, uid, name))

    async def _sign_lookup(self, bot, gk, uid, name):
        try:
            info = await asyncio.wait_for(
                bot.call_action("get_stranger_info", user_id=int(uid), no_cache=True),
                15,
            )
        except Exception as e:
            self.signs.failed(uid, time.time(), repr(e))
            self._save()
            logger.info(f"{TAG} 查 {name}({uid}) 的签名没查到（今天不再查他）：{e!r}"[:200])
            return
        try:
            now = time.time()
            tag = find_tag(sign_of(info), self._tag_rx())
            self.signs.done(uid, now, tag)
            self._save()
            if uid in self._bots():
                self._tag_seen(uid, name, gk, now, sign=tag)
            elif tag and not self.is_bot(uid) and not self.is_human(uid):
                self.mark(
                    uid,
                    name,
                    gk,
                    f"自报家门（签名）：挂着「{tag}」",
                    src="self_sign",
                    tag=tag,
                )
        except Exception as e:
            logger.warning(f"{TAG} 看 {name}({uid}) 的签名出错：{e!r}"[:200])

    # ───────── 机器人们在回指令（v0.2.0） ─────────
    def _is_echo(self, event, gk, uid, now):
        """触发这次的那条消息是不是机器人们在回指令（on_group_message 算好放在 event 上；没算过就现算）"""
        if not self._guard():
            return False
        v = event.get_extra("_fatfish_aiguard_echo")
        if v is not None:
            return bool(v)
        return cmd_echo(
            self._text(event), self.det.since_cmd(gk, now), self.det.cmd_sec
        )

    def _echo_note(self, gk, kind, text, event_text=""):
        """回指令那一阵往往一秒好几条：同一个群同一种 60 秒只记一行日志；
        给了 event_text 的（拦下了一句复读）10 分钟记一条进面板的「最近发生的事」"""
        now = time.time()
        if now - self._echo_log.get((gk, kind), 0) < 60:
            return
        self._echo_log[(gk, kind)] = now
        if event_text and now - self._echo_log.get((gk, "event"), 0) >= 600:
            self._echo_log[(gk, "event")] = now
            self._event("echo", event_text, gk)
        else:
            logger.info(text)

    @staticmethod
    def _said(res):
        try:
            return "".join(
                c.text for c in (res.chain if res else []) if isinstance(c, Comp.Plain)
            )
        except Exception:
            return ""

    def _cmd_reply(self, event, res, said):
        """这次发出去的是不是在回指令（AstrBot 自带的 /stop /help、别的插件的指令）：
        那不是你的机器人在聊天 → 不记成「你的机器人说了一句」、不数刹车轮数、不占认出来的 AI 的回复名额（v0.2.0）。
        模型回的一律算聊天（哪怕那句是 / 开头叫醒的）"""
        llm = getattr(res, "is_llm_result", None)
        if callable(llm) and llm():
            return False
        # AstrBot 认出这是一条指令、交给了哪个插件
        if event.get_extra("handlers_parsed_params"):
            return True
        return is_cmd(self._text(event)) or cmd_echo(said)

    # ───────── 每条群消息：记下来 看着像就让模型看 ─────────
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize
    )
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            self._on_group(event)
        except Exception as e:
            logger.warning(f"{TAG} 看群消息出错（这条不管了）：{e!r}"[:200])

    def _on_group(self, event):
        uid = str(event.get_sender_id())
        if self._skip(event, uid):
            return
        gk, now = self._gk(event), time.time()
        text = self._text(event)
        wake = bool(getattr(event, "is_at_or_wake_command", False))
        # 谁说的都要先过这一步（真人、管理员发的指令也算）：记下群里上一条指令的时间、这句是不是机器人们在回指令
        echo = self.det.heard(gk, uid, text, now, to_us=wake) and self._guard()
        # 后面 gate 要用：要不要拦 AstrBot 主动回复 / 别的插件接这句
        event.set_extra("_fatfish_aiguard_echo", echo)
        self.brake.heard(gk, uid, now, self.det.last_said(gk), echo=echo)
        # 发指令 / 回指令不算「冲着我们的机器人聊天」（v0.2.0：以前「/stop」也算一次 @ 它）
        to_us = wake and not echo
        # v0.4.0：谁说的都记进时间线（只记时间、谁、@ 了谁）—— 不对称分、真人信号、已知机器人 / 真人的统计都要。
        # 戳一戳这类群通知 AstrBot 也当群消息发过来：那不是在说话，不算一次出声
        if self._raw(event, "post_type") in (None, "message", "message_sent"):
            self.rh.add(
                gk,
                uid,
                now,
                self._ats(event),
                to_us=to_us,
                echo=echo,
                refs=self._refs(event),
            )
        name = event.get_sender_name() or uid
        # v0.4.0 自报家门：挂着标记的这一句就标上（后面 gate 看到是认出来的 AI，没找你的机器人就不接）
        self._self_report(event, gk, uid, name, now)
        self._maybe_sign(event, gk, uid, name, now)
        if self.is_human(uid) or self.is_bot(uid):
            # 不用再判，但记下这句：车轱辘话要看他说了什么（v0.3.0）
            self.det.note(gk, uid, text, now, to_us=to_us, echo=echo)
            return
        if self.det.observe(
            gk, uid, name, text, now, to_us, echo
        ) == "check" and self._c("enable_detect", True):
            self._spawn(self._check(gk, uid, name, event.unified_msg_origin))

    # ───────── 要叫模型回话之前：别人家的 AI / 刹车中 → 不回 ─────────
    @filter.on_waiting_llm_request(priority=sys.maxsize)
    async def gate(self, event: AstrMessageEvent):
        try:
            self._gate(event)
        except Exception as e:
            logger.warning(f"{TAG} 看这一轮要不要拦出错（照常回）：{e!r}"[:200])

    def _gate(self, event):
        if event.is_private_chat() or not event.get_group_id():
            return
        uid = str(event.get_sender_id())
        if self._skip(event, uid):
            return
        gk, now = self._gk(event), time.time()
        name = event.get_sender_name() or uid
        text = self._text(event)
        if (
            not getattr(event, "is_at_or_wake_command", False)
            and self._is_echo(event, gk, uid, now)
            and not is_cmd(text)
        ):
            # 没人找你的机器人、这句是机器人们在回指令 / 框架报错 / 指令后十几秒的齐刷刷：
            # AstrBot 的主动回复、别的插件的接话都别接（v0.2.0）。谁说的都一样 —— 真人跟着刷也不接。
            # 指令本身（「#菜单」这种没叫醒 AstrBot 的）不在这里拦：这里一拦整条消息就停了，
            # 排在后面、本来要处理这条指令的插件也收不到 —— 指令怎么处理是 AstrBot 和别的插件的事
            release_angel(self.context, event, "回指令 不接")
            event.stop_event()
            self._echo_note(
                gk, "llm", f"{TAG} {name}({uid})「{text[:20]}」是在回指令 不接"
            )
            return
        if self.is_human(uid):
            return
        if self.is_bot(uid):
            if not getattr(event, "is_at_or_wake_command", False):
                release_angel(self.context, event, "别人家的 AI 没找它")
                event.stop_event()  # 没 @ / 引用 —— 不主动接别人家 AI 的话
                logger.info(
                    f"{TAG} {name}({uid}) 是别人家的 AI 没找你的机器人 不主动接"
                )
                return
            win, lim = (
                self._c("bot_reply_window_sec", 600),
                self._c("bot_reply_limit", 3),
            )
            if self._c("brake_enable", True) and self.brake.braked(gk, uid, now):
                release_angel(self.context, event, "车轱辘话刹车中")
                event.stop_event()  # v0.3.0：跟它来回说同一套、刹车提早刹住了
                left = int((self.brake.until[(gk, uid)] - now) // 60) + 1
                logger.info(
                    f"{TAG_B} {name}({uid}) 是别人家的 AI，跟你的机器人绕成车轱辘话刹住了（还有 {left} 分钟）不回"
                )
                return
            q = [t for t in self.allowed.get((gk, uid), []) if now - t < win]
            self.allowed[(gk, uid)] = q
            if len(q) >= lim:
                release_angel(self.context, event, "别人家的 AI 回够次数了")
                event.stop_event()
                logger.info(
                    f"{TAG} {name}({uid}) 是别人家的 AI {int(win // 60)} 分钟里已经回了 {len(q)} 次 这次不回"
                )
            # 这里只看、不算：真回出去了才算一次（after_sent）—— v0.1.1：以前在这里就算上了，
            # 「攒一攒」把它连发的几条合成一次回，每条都被算了一次 名额白白用掉
            return
        if self._c("brake_enable", True) and self.brake.braked(gk, uid, now):
            release_angel(self.context, event, "对聊刹车中")
            event.stop_event()
            left = int((self.brake.until[(gk, uid)] - now) // 60) + 1
            logger.info(
                f"{TAG_B} {name}({uid}) 找你的机器人 但刹车中（还有 {left} 分钟）不回"
            )

    # ───────── 要发出去之前：别跟着机器人们回指令复读（v0.2.0） ─────────
    @filter.on_decorating_result(priority=sys.maxsize)
    async def no_echo_follow(self, event: AstrMessageEvent):
        """别的复读插件 / 主动回复要发的这句，跟群里刚刚「回指令」的那句一模一样（大家同一秒回「仅管理员可用」）→ 不发。
        只管没人找你的机器人时的；有人 @ / 引用 / 发指令时 AstrBot 和别的插件怎么回都不动"""
        try:
            self._no_echo_follow(event)
        except Exception as e:
            logger.warning(f"{TAG} 看要不要跟着复读出错（照常发）：{e!r}"[:200])

    def _no_echo_follow(self, event):
        if event.is_private_chat() or not event.get_group_id() or not self._guard():
            return
        if getattr(event, "is_at_or_wake_command", False):
            return
        gs = self._cfg_list("groups")
        if gs and str(event.get_group_id()) not in gs:
            return
        said = self._said(event.get_result())
        gk, now = self._gk(event), time.time()
        if not said.strip() or not self.det.echo_repeat(gk, said, now):
            return
        # 清空结果不发 = 不会有 after_message_sent：天使之心放行过的这一轮先替它记成结束（跟别处拦下时一样）
        release_angel(self.context, event, "跟着回指令复读 不发")
        event.clear_result()  # 结果清空 = 这句不发（也不会触发 after_message_sent）
        self._echo_note(
            gk,
            "repeat",
            f"{TAG} 群里机器人们在回指令「{said[:20]}」→ 不跟着复读",
            f"群 {gk.split(':', 1)[-1]} 机器人们在回指令「{said[:20]}」→ 没跟着复读（10 分钟只记一条）",
        )

    # ───────── 回完一句：记下来、数来回的轮数 ─────────
    @filter.after_message_sent()
    async def after_sent(self, event: AstrMessageEvent):
        try:
            self._after_sent(event)
        except Exception as e:
            logger.warning(f"{TAG} 记你的机器人说了一句出错：{e!r}"[:200])

    def _after_sent(self, event):
        if event.is_private_chat() or not event.get_group_id():
            return
        gk, now = self._gk(event), time.time()
        res = event.get_result()
        said = self._said(res)
        if self._guard() and self._cmd_reply(event, res, said):
            # 回的是指令：不算你的机器人在聊天（v0.2.0：以前「/stop」回一句「No running tasks」也记成说了一句 ——
            # 别家的机器人同一秒回指令就被算成「秒接」；发指令的人也被多数了一轮来回）
            return
        uid = str(event.get_sender_id())
        # 回的是谁也记上（v0.3.0：车轱辘话只看回他的那几句）
        self.det.our_said(gk, now, said, "" if self._skip(event, uid) else uid)
        gs = self._cfg_list("groups")
        if not gs or str(event.get_group_id()) in gs:
            # v0.4.0：你的机器人说话的时间也进时间线（别人「说完几秒就接」是从它这句算起）
            self.rh.add(gk, RH_SELF, now)
        if not self._skip(event, uid) and self.is_bot(
            uid
        ):  # 回了别人家的 AI 一次：记进「10 分钟最多 3 次」（v0.1.1）
            win = self._c("bot_reply_window_sec", 600)
            q = [t for t in self.allowed.get((gk, uid), []) if now - t < win]
            self.allowed[(gk, uid)] = q + [now]
            self._loop_brake(event, gk, uid, now)
            return
        if (
            self._skip(event, uid)
            or self.is_human(uid)
            or self.is_bot(uid)
            or not self._c("brake_enable", True)
        ):
            return
        name = event.get_sender_name() or uid
        r = self.brake.replied(
            gk, uid, now, self.det.evidence(gk, uid, now, self.brake.window)
        )
        if not r:
            return
        kind, n = r
        if kind == "human":
            logger.info(
                f"{TAG_B} {name} 跟你的机器人来回 {n} 轮 但看着像真人在聊（没有几秒内连发、没自称、没框架输出）不刹"
            )
            return
        self._event(
            "brake",
            f"{name}({uid}) {int(self.brake.window // 60)} 分钟里跟你的机器人来回 {n} 轮 每次都是它说完 "
            f"{int(self.brake.fast)} 秒内就接 → 先停 {int(self.brake.pause // 60)} 分钟不回他；叫模型再看一眼",
            gk,
            uid,
        )
        if self._c("enable_detect", True):
            self._spawn(
                self._check(gk, uid, name, event.unified_msg_origin, force=True)
            )

    def _loop_brake(self, event, gk, uid, now):
        """回了认出来的 AI 一次（v0.3.0）：跟它来回说同一套了 → 对聊刹车提早（二档够 2 轮就刹、慢的来回也算；一档轮数减半）。
        没绕起来的照旧只有「10 分钟最多 3 次」（8 轮秒接到不了）"""
        if not (self._c("brake_enable", True) and self._loop_on()):
            return
        name = event.get_sender_name() or uid
        st = self.det.loop(gk, uid, now, [name], fish=True)
        lv = int((st or {}).get("level") or 0)
        r = self.brake.replied(
            gk,
            uid,
            now,
            self.det.evidence(gk, uid, now, self.brake.window),
            is_bot=True,
            lv=lv,
        )
        if not r or r[0] != "brake":
            return
        ph = "、".join(g for g, _a, _b in (st or {}).get("phrases", [])[:3])
        self._event(
            "brake",
            f"{name}({uid}) 是别人家的 AI，跟你的机器人来回说同一套（{ph}；{(st or {}).get('rounds', 0)} 轮，第 {lv} 档）"
            f"→ 够 {self.brake.need(lv)} 轮就提早刹：先停 {int(self.brake.pause // 60)} 分钟不回它",
            gk,
            uid,
        )

    # ───────── 要叫模型之前：车轱辘话的状态（v0.3.0） ─────────
    @filter.on_llm_request()
    async def loop_note(self, event: AstrMessageEvent, req: ProviderRequest):
        """你的机器人跟这个人来回说同一套 → 这一轮给模型一段状态（只写事实）；
        对面是认出来的 AI、还没绕起来 → 先提醒别学它的说法。都是这一轮临时加的，不进聊天记录"""
        try:
            self._loop_note(event, req)
        except Exception as e:
            logger.warning(f"{TAG} 车轱辘话的状态没加上：{e!r}"[:200])

    def _loop_note(self, event, req):
        if TextPart is None or not self._loop_on():
            return
        if event.is_private_chat() or not event.get_group_id():
            return
        uid = str(event.get_sender_id())
        if self._skip(event, uid):
            return
        gk, now = self._gk(event), time.time()
        name = event.get_sender_name() or uid
        fish = self.is_bot(uid)
        st = self.det.loop(gk, uid, now, [name], fish=fish)
        lv = int((st or {}).get("level") or 0)
        if lv:
            text = loop_block(st, name)
            sig = (gk, uid, lv)
            if text and now - self._loop_log.get(sig, 0) > 300:
                self._loop_log[sig] = now
                while len(self._loop_log) > 500:
                    self._loop_log.pop(next(iter(self._loop_log)))
                logger.info(
                    f"{TAG} 车轱辘话：{name}({uid}){'（别人家的 AI）' if fish else ''} {st['mins']} 分钟里跟你的机器人来回 "
                    f"{st['rounds']} 轮同一套（"
                    + "、".join(f"{g} {a}/{b}" for g, a, b in st["phrases"][:3])
                    + f"；停滞 {st['stag']}）→ 第 {lv} 档，这一轮给模型状态"
                )
        elif fish:
            text = fish_note(name)
        else:
            return
        if text:
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())   # v0.3.1：临时段，不写进对话记录（11-08 隔壁窗口查出）

    # ───────── 让模型看 ─────────
    async def _llm(self, umo, body):
        pid = str(self.config.get("judge_provider_id") or "").strip()
        if not pid:
            pid = await self.context.get_current_chat_provider_id(umo)
        resp = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=pid, prompt=body, system_prompt=JUDGE
            ),
            60,
        )
        return getattr(resp, "completion_text", "") or ""

    async def _check(self, gk, uid, name, umo, force=False):
        k = (gk, uid)
        if k in self.det.busy:
            return None
        day = time.strftime("%Y-%m-%d")
        if self.judged_today[0] != day:
            self.judged_today = [day, 0]
        if self.judged_today[1] >= self._c("judge_daily_max", 300):
            logger.info(
                f"{TAG} 今天已经让模型看了 {self.judged_today[1]} 次（上限 judge_daily_max）{name} 这次不看"
            )
            return None
        self.det.busy.add(k)
        now = time.time()
        try:
            if force:
                self.det.asked[str(uid)] = (
                    now,
                    self.det.evidence(gk, uid, now)["score"],
                )
            body = self.det.judge_body(gk, uid, name, now)
            if not body:
                return None
            self.judged_today[1] += 1
            try:
                out = await self._llm(umo, body)
            except Exception as e:
                logger.warning(f"{TAG} 让模型看 {name} 失败：{e!r}"[:200])
                return None
            verdict = Detector.parse(out)
            t2 = time.time()
            # v0.4.0 真人信号：有的话「光靠行为」那几条路不标（只记「观察」）；模型把握够的照标
            veto = (
                self.rh.signals(gk, uid, t2, is_bot=self.is_bot)
                if self._c("human_veto", True)
                else []
            )
            # v0.4.0 不对称分：默认影子模式（只记不判），算一下「要是算进去会怎样」
            af = self.rh.asym(gk, uid, t2)
            a = af["score"] if af else None
            on = bool(self._c("asym_in_judge", False))
            kind, ev, why = self.det.decide(
                uid,
                name,
                gk,
                verdict,
                t2,
                self._c("judge_threshold", 75),
                self._c("judge_threshold_low", 65),
                self._c("judge_low_min_score", 8),
                veto=veto,
                asym=a,
                asym_plus=asym_bonus(a),
                asym_on=on,
            )
            self._save()
            if af:
                alt = self.det.store["verdicts"].get(str(uid), {}).get("asym_if")
                logger.info(
                    f"{TAG} 不对称分（{'参与判断' if on else '影子模式 只记不判'}）：{name}({uid}) {asym_text(af)}"
                    f" → {'不算的话' if on else '要是算进去（行为分 +' + str(asym_bonus(a)) + '）'}："
                    f"{KIND_TEXT.get(alt, alt)}（这次：{KIND_TEXT.get(kind, kind)}）"
                )
            if kind == "mark":
                b = self._bots().get(str(uid)) or {}
                if str(b.get("src") or "").startswith("self_"):
                    return True  # 判的这一会儿它自报家门了：留着自报家门的记录（看它摘没摘要用）
                self.mark(uid, name, gk, why)
                return True
            if kind == "watch":
                self._event("watch", f"{name}({uid}) {why}", gk, uid)
                return False
            if (
                kind == "human"
                and force
                and verdict[1] >= self._c("judge_threshold", 75)
                and self.brake.release(gk, uid)
            ):
                self._event(
                    "release",
                    f"{name}({uid}) 模型看了是真人（把握 {verdict[1]}）→ 刹车放开 照常回",
                    gk,
                    uid,
                )
            else:
                logger.info(f"{TAG} {name}({uid}) {why}")
            return False
        finally:
            self.det.busy.discard(k)

    # ───────── 管理员指令 ─────────
    @filter.command_group("认ai", alias={"认AI"})
    def aiguard(self):
        pass

    @filter.permission_type(filter.PermissionType.ADMIN)
    @aiguard.command("列表")
    async def cmd_list(self, event: AstrMessageEvent):
        """认出来的别人家的 AI"""
        bots = self._bots()
        manual = self._cfg_list("always_bot")
        if not bots and not manual:
            yield event.plain_result("还没认出别人家的 AI。")
            return
        rows = [
            f"{v.get('name') or '?'}({k})：{v.get('why', '')[:40]}"
            + ("（标记已摘）" if v.get("tag_gone") else "")
            for k, v in list(bots.items())[-30:]
        ]
        rows += [f"({k})：配置里手动填的" for k in manual if k not in bots]
        yield event.plain_result("认出来的别人家的 AI：\n" + "\n".join(rows))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @aiguard.command("真人")
    async def cmd_human(self, event: AstrMessageEvent, qq: str = ""):
        """把认错的号改回真人：/认ai 真人 <QQ号>"""
        qq = qq.strip()
        if not qq.isdigit():
            yield event.plain_result("用法：/认ai 真人 <QQ号>")
            return
        self.unmark(qq, f"管理员 {event.get_sender_name()} 说是真人")
        self._save(force=True)
        yield event.plain_result(f"好，{qq} 以后当真人，不再自动标。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @aiguard.command("标记")
    async def cmd_mark(self, event: AstrMessageEvent, qq: str = ""):
        """手动标成别人家的 AI：/认ai 标记 <QQ号>"""
        qq = qq.strip()
        if not qq.isdigit():
            yield event.plain_result("用法：/认ai 标记 <QQ号>")
            return
        self.mark(
            qq,
            qq,
            self._gk(event),
            f"管理员 {event.get_sender_name()} 手动标的",
            auto=False,
        )
        self._save(force=True)
        yield event.plain_result(
            f"好，{qq} 标成别人家的 AI 了：以后只有 @ / 引用才回，10 分钟最多 {self._c('bot_reply_limit', 3)} 次。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @aiguard.command("看看")
    async def cmd_check(self, event: AstrMessageEvent, qq: str = ""):
        """让模型马上看一眼这个号：/认ai 看看 <QQ号>（要在它说过话的群里发）"""
        qq = qq.strip()
        gk = self._gk(event)
        q = self.det.msgs.get((gk, qq))
        if not qq.isdigit() or not q:
            yield event.plain_result(
                "这个号在这个群里还没说过话（或者用法不对：/认ai 看看 <QQ号>）。"
            )
            return
        name = (self.det.store["verdicts"].get(qq) or {}).get("name") or qq
        marked = await self._check(gk, qq, name, event.unified_msg_origin, force=True)
        v = self.det.store["verdicts"].get(qq) or {}
        w = (v.get("watch") or {}).get("why") if not marked else ""
        yield event.plain_result(
            (
                "标成别人家的 AI 了"
                if marked
                else f"没标，只记「观察」（有真人信号：{w}）"
                if w
                else "没标"
            )
            + f"（模型：{'是 AI' if v.get('bot') else '像真人'} 把握 {v.get('conf', '?')}，{v.get('why', '')}；"
            f"行为分 {v.get('score', '?')}）"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @aiguard.command("刹车")
    async def cmd_brakes(self, event: AstrMessageEvent):
        """现在刹着的人"""
        now = time.time()
        rows = [
            f"{uid}（群 {gk.split(':', 1)[-1]}，还有 {int((t - now) // 60) + 1} 分钟）"
            for (gk, uid), t in self.brake.until.items()
            if t > now
        ]
        yield event.plain_result(
            "现在刹着：\n" + "\n".join(rows) if rows else "现在没刹着谁。"
        )

    # ───────── 一键给自己挂上 / 摘掉自报家门的标记（v0.4.0） ─────────
    def _tagged(self):
        """挂标记时存下的原名片 / 签名（摘的时候原样改回去）"""
        return self.state.setdefault("self_tag", {})

    def _put_rx(self):
        """「已经带着标记」按配置的写法认，再加上约定的 [bot]（配置清空了也别重复加）"""
        return tag_rx(
            tag_list(self.config.get("self_report_tags", list(DEFAULT_TAGS)))
            + [SELF_TAG]
        )

    @staticmethod
    def _caller(event):
        """调 NapCat 接口（aiocqhttp 的 call_action）；不是 QQ 平台发的指令 → None"""
        fn = getattr(getattr(event, "bot", None), "call_action", None)
        if not callable(fn):
            return None

        async def call(action, **kw):
            # NapCat 改群名片要等它自己确认生效（最多三四秒），给足时间
            return await asyncio.wait_for(fn(action, **kw), 20)

        return call

    @staticmethod
    def _gl(gs, n=20):
        gs = list(gs)
        return "、".join(gs[:n]) + (f" 等 {len(gs)} 个" if len(gs) > n else "")

    async def _dm(self, event, text):
        """私聊发指令的管理员一句（系统口吻）→ 发出去了没有"""
        try:
            await self.context.send_message(
                f"{event.get_platform_id()}:FriendMessage:{event.get_sender_id()}",
                MessageChain([Comp.Plain(text=text)]),
            )
            return True
        except Exception as e:
            logger.info(f"{TAG} 私聊管理员没发出去：{e!r}"[:160])
            return False

    async def _tag_answer(self, event, rep, on):
        """挂 / 摘的结果怎么回：私聊里发的指令直接回明细；在群里发的，群里只回个数，群号、名片的明细私聊给他
        （11-08 复查：机器人在哪些群、名片叫什么，别晒给这个群的群友）"""
        short, detail, has = tag_report(rep, on=on)
        if event.is_private_chat() or not event.get_group_id():
            return detail
        if not has:
            return short
        what = "挂标记" if on else "摘标记"
        if await self._dm(event, f"（系统提示）认 AI {what}的明细：\n{detail}"):
            return short + "明细私聊给你了。"
        return short + "明细私聊没发出去（加机器人好友再发一次，或者去插件面板「你的机器人挂着的标记」看）。"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @aiguard.command("挂标记")
    async def cmd_tag_on(self, event: AstrMessageEvent):
        """给你的机器人挂上自报家门的标记（群名片后面加「 [bot]」，可选签名也加）：/认ai 挂标记（配置里先打开）"""
        if not self._c("self_tag_enable", False):
            yield event.plain_result(
                "一键挂标记默认关着：插件配置里打开「一键给自己挂标记」再发一次。"
                f"也可以自己挂：把「 {SELF_TAG}」加在你的机器人的群名片 / 昵称 / 签名后面。"
            )
            return
        call, sid = self._caller(event), str(event.get_self_id())
        if call is None or not sid.isdigit():
            yield event.plain_result("这个指令要在 QQ 里发（OneBot v11 / NapCat）。")
            return
        gids = sorted(self._cfg_list("self_tag_groups")) or sorted(
            self._cfg_list("groups")
        )
        sign = bool(self._c("self_tag_sign", False))

        async def notify(gs, sign_too):
            # 改之前先私聊发指令的管理员：改的是机器人在别的群的样子，群里的人看得见
            parts = []
            if gs:
                parts.append(f"这 {len(gs)} 个群的群名片后面加「 {SELF_TAG}」：群 {self._gl(gs)}")
            if sign_too:
                parts.append(f"签名后面也加「 {SELF_TAG}」")
            await self._dm(
                event,
                "（系统提示）认 AI 马上给你的机器人挂上自报家门的标记："
                + "；".join(parts)
                + "。原来的名片 / 签名都存下了，发「/认ai 摘标记」原样改回去。",
            )

        try:
            # 每改一个群之前先把原名片存到硬盘上（persist）；没配群 = 所有群时一次最多改 ALL_MAX 个
            rep = await put_tags(
                call,
                sid,
                gids,
                self._put_rx(),
                self._tagged(),
                sign=sign,
                notify=notify,
                gap=1.0,
                persist=self._persist,
            )
        except Exception as e:
            self._save(force=True)
            yield event.plain_result(f"没挂成：{e!r}"[:200])
            return
        self._save(force=True)
        st = rep["sign"]
        if rep["done"] or rep["fail"] or (st and st != "had"):
            self._event(
                "selftag",
                f"给你的机器人挂自报家门的标记：群名片改了 {len(rep['done'])} 个群"
                + ("、签名也加了" if st == "done" else "")
                + (f"；{len(rep['fail'])} 个群没改成" if rep["fail"] else "")
                + ("；签名没改成" if st.startswith("fail") else "")
                + (f"；还有 {rep['left']} 个群这次没看（一次最多改一批）" if rep.get("left") else ""),
            )
        yield event.plain_result(await self._tag_answer(event, rep, True))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @aiguard.command("摘标记")
    async def cmd_tag_off(self, event: AstrMessageEvent):
        """把 /认ai 挂标记 改的群名片 / 签名原样改回去"""
        saved = self._tagged()
        if not saved.get("groups") and not saved.get("sign"):
            yield event.plain_result("没有用「/认ai 挂标记」挂过（自己手动挂的请自己摘）。")
            return
        call, sid = self._caller(event), str(event.get_self_id())
        if call is None or not sid.isdigit():
            yield event.plain_result("这个指令要在 QQ 里发（OneBot v11 / NapCat）。")
            return
        try:
            rep = await take_tags(call, sid, saved, gap=1.0, persist=self._persist)
        except Exception as e:
            self._save(force=True)
            yield event.plain_result(f"没摘成：{e!r}"[:200])
            return
        self._save(force=True)
        st = rep["sign"]
        self._event(
            "selftag",
            f"摘你的机器人自报家门的标记：群名片改回了 {len(rep['done'])} 个群"
            + ("、签名也改回了" if st == "done" else "")
            + (f"；{len(rep['changed'])} 个群后来改过没动" if rep["changed"] else "")
            + (f"；{len(rep['fail'])} 个群没改成" if rep["fail"] else ""),
        )
        yield event.plain_result(await self._tag_answer(event, rep, False))

    # ───────── 面板（AstrBot WebUI → 插件 → 打开页面） ─────────
    def _register_web(self):
        import importlib.util

        try:
            if (
                importlib.util.find_spec("astrbot.api.web") is None
            ):  # 4.29 以后才有：老版本就不开面板
                return
        except Exception:
            return
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/state", self.web_state, ["GET"], "认 AI：状态"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/action", self.web_action, ["POST"], "认 AI：改名单"
        )

    def _asym_now(self, now):
        """每个人现在的不对称分（他出声最多的那个群的）→ {QQ: 特征}。
        一个群扫一遍、60 秒内的直接用上次算的（11-08 复查：面板每 15 秒要一次，以前挨个人各扫一遍，
        100 个群 × 800 句要将近一秒，卡住事件循环）"""
        return self.rh.best_all(now)

    def _asym_stats(self, best):
        """拿已经认出来的 AI、真人名单（含 AstrBot 管理员、配置里永远当真人的）当现成标注：这个分分得开不开"""
        bots = [f["score"] for u, f in best.items() if self.is_bot(u)]
        humans = [
            f["score"] for u, f in best.items() if not self.is_bot(u) and self.is_human(u)
        ]
        return separation(bots, humans)

    def snapshot(self):
        now = time.time()
        vs = self.det.store.get("verdicts") or {}
        recent = sorted(
            ((k, v) for k, v in vs.items() if v.get("t")),
            key=lambda kv: -float(kv[1].get("t") or 0),
        )[:40]
        best = self._asym_now(now)
        tagged = self._tagged()
        return {
            "bots": [dict(uid=k, **v) for k, v in self._bots().items()],
            "manual_bots": sorted(self._cfg_list("always_bot")),
            "humans": [dict(uid=k, **v) for k, v in self._humans().items()],
            "verdicts": [
                {
                    "uid": k,
                    "name": v.get("name", ""),
                    "bot": bool(v.get("bot")),
                    "conf": v.get("conf", 0),
                    "why": v.get("why", ""),
                    "score": v.get("score", 0),
                    "mixed": bool(v.get("mixed")),
                    "t": int(v.get("t") or 0),
                    "fw": v.get("fw", 0),
                    # v0.4.0：真人信号否决了的（只记「观察」）、不对称分（判的时候的 / 现在的）和「开关另一边会怎样」
                    "watch": (v.get("watch") or {}).get("why", ""),
                    "asym": v.get("asym"),
                    "asym_now": (best.get(k) or {}).get("score"),
                    "asym_txt": asym_text(best.get(k)) if best.get(k) else "",
                    "asym_if": KIND_TEXT.get(v.get("asym_if"), "")
                    if v.get("asym_if")
                    else "",
                    "asym_plus": v.get("asym_plus", 0),
                }
                for k, v in recent
            ],
            "asym": {
                "on": bool(self._c("asym_in_judge", False)),
                "min": ASYM_MIN,
                "bonus": ASYM_BONUS,
                "stats": self._asym_stats(best),
            },
            "signs": {
                "on": bool(self._c("self_report", True))
                and bool(self._c("self_report_sign", True)),
                "today": self.signs.today(now),
            },
            "self_tag": {
                "groups": [
                    {"g": g, "old": r.get("old", ""), "new": r.get("new", ""), "ok": bool(r.get("ok"))}
                    for g, r in sorted((tagged.get("groups") or {}).items())
                ],
                "sign": bool(tagged.get("sign")),
            },
            "brakes": [
                {"g": gk.split(":", 1)[-1], "uid": uid, "left": int(t - now)}
                for (gk, uid), t in self.brake.until.items()
                if t > now
            ],
            "events": list(self.events)[-60:][::-1],
            "judged_today": self.judged_today[1],
            "config": {
                k: self.config.get(k)
                for k in (
                    "brake_rounds",
                    "brake_window_sec",
                    "brake_pause_sec",
                    "brake_fast_sec",
                    "bot_reply_limit",
                    "bot_reply_window_sec",
                    "judge_threshold",
                    "cmd_echo_guard",
                    "cmd_echo_sec",
                    "loop_enable",
                    "self_report",
                    "self_report_tags",
                    "self_report_sign",
                    "self_tag_enable",
                    "asym_in_judge",
                    "human_veto",
                )
            },
        }

    async def web_state(self):
        try:
            return {"status": "ok", "data": self.snapshot()}
        except Exception as e:
            logger.warning(f"{TAG} 面板读状态出错：{e!r}"[:200])
            return {"status": "error", "message": f"读状态出错：{e!r}"[:200]}

    async def web_action(self):
        from astrbot.api.web import request

        body = await request.json({}) or {}
        op, uid = str(body.get("op") or ""), str(body.get("uid") or "").strip()
        if not uid.isdigit():
            return {"status": "error", "message": "QQ 号不对"}
        if op == "human":
            self.unmark(uid, "面板里改成真人")
        elif op == "bot":
            name = (self.det.store["verdicts"].get(uid) or {}).get("name") or uid
            self.mark(uid, name, "", "面板里手动标的", auto=False)
        elif op == "release":
            for k in [k for k in self.brake.until if k[1] == uid]:
                self.brake.until.pop(k, None)
            self._event("release", f"{uid} 面板里解除刹车", "", uid)
        elif op == "forget":
            self._humans().pop(uid, None)
            self.det.store["verdicts"].pop(uid, None)
            self._event("forget", f"{uid} 面板里清掉判断记录（以后重新看）", "", uid)
        else:
            return {"status": "error", "message": "不认识的操作"}
        self._save(force=True)
        return {"status": "ok", "data": self.snapshot()}
