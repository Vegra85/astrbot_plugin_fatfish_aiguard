# -*- coding: utf-8 -*-
"""
大肥鱼 · 认 AI + 对聊刹车（AstrBot 插件）

不用填名单：群里别人部署的 AI 聊天机器人，它自己认出来；认出来以后只有 @ / 引用你的机器人才回，10 分钟最多回 3 次，
不会被它拖进停不下来的对骂 / 互道晚安。还没认出来之前，对聊刹车兜底：来回太快太多、对方又像机器人 → 先停 10 分钟。
判断逻辑在 detector.py（不依赖 AstrBot，可以单独测）。
"""

import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp

from .detector import JUDGE, Brake, Detector

PLUGIN_NAME = "astrbot_plugin_fatfish_aiguard"
TAG = "[认AI]"
TAG_B = "[对聊刹车]"


def _num(v, d):
    try:
        return type(d)(v)
    except (TypeError, ValueError):
        return d


class FatfishAIGuard(Star):
    """认别人家的 AI + 对聊刹车。管理员指令：/认ai 列表 | 真人 <QQ> | 标记 <QQ> | 看看 <QQ> | 刹车"""

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
        self._dirty = False
        self._tasks = set()
        self._saver = None
        self._register_web()

    # ───────── 配置 / 存档 ─────────
    def _c(self, key, default):
        return _num(self.config.get(key, default), default)

    def _load(self):
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self, force=False):
        self._dirty = True
        if not force:
            return
        try:
            self.state["events"] = list(self.events)[-100:]
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.state_file)
            self._dirty = False
        except Exception as e:
            logger.warning(f"{TAG} 存档失败：{e!r}")

    async def _save_loop(self):
        while True:
            await asyncio.sleep(15)
            if self._dirty:
                self._save(force=True)

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        if self._saver is None or self._saver.done():
            self._saver = asyncio.create_task(self._save_loop())

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

    def mark(self, uid, name, gk, why, auto=True):
        self._bots()[str(uid)] = {
            "name": name,
            "why": why,
            "t": int(time.time()),
            "g": gk,
            "auto": auto,
        }
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
    def _text(event):
        """这条消息的文字：优先用 QQ 原始消息段（保留换行 —— 「@人 换行 说话」是 bot 框架的格式）"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        segs = None
        try:
            segs = (
                raw.get("message")
                if hasattr(raw, "get")
                else getattr(raw, "message", None)
            )
        except Exception:
            segs = None
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

    # ───────── 每条群消息：记下来 看着像就让模型看 ─────────
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize
    )
    async def on_group_message(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        if self._skip(event, uid):
            return
        gk, now = self._gk(event), time.time()
        self.brake.heard(gk, uid, now, self.det.last_said(gk))
        if self.is_human(uid) or self.is_bot(uid):
            return
        to_us = bool(getattr(event, "is_at_or_wake_command", False))
        name = event.get_sender_name() or uid
        if self.det.observe(
            gk, uid, name, self._text(event), now, to_us
        ) == "check" and self._c("enable_detect", True):
            self._spawn(self._check(gk, uid, name, event.unified_msg_origin))

    # ───────── 要叫模型回话之前：别人家的 AI / 刹车中 → 不回 ─────────
    @filter.on_waiting_llm_request(priority=sys.maxsize)
    async def gate(self, event: AstrMessageEvent):
        if event.is_private_chat() or not event.get_group_id():
            return
        uid = str(event.get_sender_id())
        if self._skip(event, uid) or self.is_human(uid):
            return
        gk, now = self._gk(event), time.time()
        name = event.get_sender_name() or uid
        if self.is_bot(uid):
            if not getattr(event, "is_at_or_wake_command", False):
                event.stop_event()  # 没 @ / 引用 —— 不主动接别人家 AI 的话
                logger.info(
                    f"{TAG} {name}({uid}) 是别人家的 AI 没找你的机器人 不主动接"
                )
                return
            win, lim = (
                self._c("bot_reply_window_sec", 600),
                self._c("bot_reply_limit", 3),
            )
            q = [t for t in self.allowed.get((gk, uid), []) if now - t < win]
            self.allowed[(gk, uid)] = q
            if len(q) >= lim:
                event.stop_event()
                logger.info(
                    f"{TAG} {name}({uid}) 是别人家的 AI {int(win // 60)} 分钟里已经回了 {len(q)} 次 这次不回"
                )
            # 这里只看、不算：真回出去了才算一次（after_sent）—— v0.1.1：以前在这里就算上了，
            # 「攒一攒」把它连发的几条合成一次回，每条都被算了一次 名额白白用掉
            return
        if self._c("brake_enable", True) and self.brake.braked(gk, uid, now):
            event.stop_event()
            left = int((self.brake.until[(gk, uid)] - now) // 60) + 1
            logger.info(
                f"{TAG_B} {name}({uid}) 找你的机器人 但刹车中（还有 {left} 分钟）不回"
            )

    # ───────── 回完一句：记下来、数来回的轮数 ─────────
    @filter.after_message_sent()
    async def after_sent(self, event: AstrMessageEvent):
        if event.is_private_chat() or not event.get_group_id():
            return
        gk, now = self._gk(event), time.time()
        res = event.get_result()
        said = ""
        try:
            said = "".join(
                c.text for c in (res.chain if res else []) if isinstance(c, Comp.Plain)
            )
        except Exception:
            said = ""
        self.det.our_said(gk, now, said)
        uid = str(event.get_sender_id())
        if not self._skip(event, uid) and self.is_bot(
            uid
        ):  # 回了别人家的 AI 一次：记进「10 分钟最多 3 次」（v0.1.1）
            win = self._c("bot_reply_window_sec", 600)
            q = [t for t in self.allowed.get((gk, uid), []) if now - t < win]
            self.allowed[(gk, uid)] = q + [now]
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
            kind, ev, why = self.det.decide(
                uid,
                name,
                gk,
                verdict,
                time.time(),
                self._c("judge_threshold", 75),
                self._c("judge_threshold_low", 65),
                self._c("judge_low_min_score", 8),
            )
            self._save()
            if kind == "mark":
                self.mark(uid, name, gk, why)
                return True
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
        yield event.plain_result(
            ("标成别人家的 AI 了" if marked else "没标")
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

    def snapshot(self):
        now = time.time()
        vs = self.det.store.get("verdicts") or {}
        recent = sorted(
            ((k, v) for k, v in vs.items() if v.get("t")),
            key=lambda kv: -float(kv[1].get("t") or 0),
        )[:40]
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
                }
                for k, v in recent
            ],
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
                )
            },
        }

    async def web_state(self):
        return {"status": "ok", "data": self.snapshot()}

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
