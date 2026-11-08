#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试：真的 AstrBot（测过 4.29.3、4.29.4）+ 这个插件 + 假 QQ 端（假 NapCat 反向 WebSocket）+ 假模型（OpenAI 兼容）
  A 真人老王连着 @ 机器人 12 次（每次隔几秒、一次一句）→ 每次都回、不刹、不叫模型看
  B 「隔壁的抹香鲸」（自称本小姐、漏 <h> 标签、两条连发）：先在群里自己说几句 → 插件让模型看（假模型：AI 把握 72）→ 标上；
    之后它 @ 机器人 6 次 → 10 分钟里只回 3 次
  C 「小鲸」（假模型说像真人 55）：@ 机器人、机器人一说完 1 秒就接两条 → 第 8 轮刹住 → 后面不回
  D 管理员指令：/认ai 列表、/认ai 真人 <QQ>（改回真人以后抹香鲸 @ 又会回了）；不是管理员 → 没权限
  E 面板接口：/api/v1/plugins/extensions/<插件>/state 能读到；/action 能改
  G 机器人们在回指令（v0.2.0）：这个群开了 AstrBot 的主动回复（概率 1）、装了一个测试用的复读插件；
    别家的机器人发「/stop」→ AstrBot 照常回指令；三个机器人同一秒回「管理命令仅管理员可用。」→ 不跟着复读、主动回复也不接；
    过了 15 秒普通的话照常接；让模型看那几个号时：回指令的那句不算秒接、「/stop」不算 @ 它、AstrBot 回的指令不算机器人说的话
  H 车轱辘话（v0.3.0）：真人「阿循」跟机器人来回说同一套 8 轮 → 每轮照常回；来回几轮以后，这一轮给模型的请求里带上状态
    「你们在来回说同一套」（哪几个说法、各几次；绕够了写「已经是车轱辘话了」）；对面是认出来的 AI（抹香鲸）→ 每次回它都先提醒别学它的说法；
    真人（老王）的请求里两样都没有
  I 自报家门（v0.4.0）：这个群开着主动回复；群名片「阿珍 [bot]」的号第一句（没 @）就认出来、主动回复不接、没调模型；
    昵称「小橙（Bot）」的也认；「robot 爱好者」不认；签名里挂着「［BOT］」的号：后台查一次签名就认出来，说好几句也只查一次；
    阿珍把标记摘了 → 不撤、面板标「标记已摘」；管理员把小橙撤成真人 → 昵称还挂着也不再认
  J 一键挂标记（v0.4.0，配置里打开）：/认AI 挂标记 → 先私聊管理员说改哪几个群，再调假 NapCat 的 set_group_card / set_self_longnick
    （名片空着的用昵称加、已经带着的不重复加）；群里只回个数，群号、名片的明细私聊；再发一次不重复改；
    /认AI 摘标记 → 名片、签名原样恢复（群里也只回个数）
  K 真人信号一票否决（v0.4.0）：「阿木」先 @ 了三个不同的人，再跟机器人秒接连发停不下来 → 照旧刹车、叫模型看（假模型：AI 把握 70）
    → 行为分够，但有真人信号：不自动标，面板里记「观察」，日志写清楚是哪条信号；不对称分（影子模式）日志里记了一行
  F 重启以后名单还在
用法：python tests/e2e_astrbot.py [插件目录 默认上一层] [--keep]
  需要装了 astrbot（测过 4.29.3、4.29.4）的 Python，astrbot 命令在 PATH 里；另外要 aiohttp、pyjwt、websockets（装 astrbot 时都会带上）
  只在临时文件夹里跑：模型是假的、QQ 端是假的，不连外网
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import jwt
import websockets

PLUGIN_SRC = Path(
    sys.argv[1]
    if len(sys.argv) > 1 and not sys.argv[1].startswith("--")
    else Path(__file__).resolve().parent.parent
).resolve()
KEEP = "--keep" in sys.argv
SLOW = max(1.0, float(os.environ.get("E2E_SLOW") or 1))
PNAME = PLUGIN_SRC.name
SELF, ADMIN, HUMAN, WHALE, KUN, OTHER = 99999, 10001, 30001, 40001, 40002, 50005
CMDBOT, BOT_A, BOT_B, BOT_C = (
    60001,
    60002,
    60003,
    60004,
)  # G：发指令的、回指令的几个机器人（号都是编的）
G1, G2, G3, G4 = 7001, 7002, 7003, 7004
G5, LOOPER = 7005, 30005  # H：跟机器人来回说同一套的真人（号是编的）
G6, G7 = 7006, 7007  # I：自报家门（开着主动回复）；K：真人信号一票否决
BOTCARD, BOTNICK, ROBOFAN, SIGNBOT = 70001, 70002, 70003, 70004  # I（号都是编的）
TALKER = 30006  # K：秒接多但 @ 过三个不同的人的真人
SELF_CARDS = {G1: "大肥鱼", G2: "", G3: "大肥鱼 [bot]"}  # J：你的机器人在这几个群原来的名片
SELF_SIGN = "今天也要加油"
RES = []


def check(cond, msg, why=""):
    RES.append((bool(cond), msg))
    print(
        ("  [过]   " if cond else "  [没过] ")
        + msg
        + ("" if cond or not why else f"\n           {str(why)[:800]}"),
        flush=True,
    )
    return bool(cond)


def real_tracebacks(text):
    """日志里的 Traceback，去掉 AstrBot 自带「主动回复」被别的插件拦下时它自己打的那段：
    它的 on_message 用 except BaseException 把 GeneratorExit 也当成出错记了一笔（「主动回复失败」）。
    v0.1.1 拦认出来的 AI 时就一样，只是以前的测试没开主动回复"""
    out = []
    for chunk in text.split("Traceback (most recent call last):")[1:]:
        head = chunk[:1500]
        if "GeneratorExit" in head and "主动回复失败" in head:
            continue
        out.append(chunk[:1200])
    return out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LLM_PORT, WS_PORT, DASH_PORT = free_port(), free_port(), free_port()
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-"))
judge_calls, chat_calls = [], []


# ───────────── 假模型 ─────────────
async def chat_completions(request):
    body = await request.json()
    msgs = body.get("messages") or []
    sysmsg = " ".join(
        m.get("content")
        if isinstance(m.get("content"), str)
        else json.dumps(m.get("content"), ensure_ascii=False)
        for m in msgs
        if m.get("role") == "system"
    )
    last = msgs[-1].get("content") if msgs else ""
    last = last if isinstance(last, str) else json.dumps(last, ensure_ascii=False)
    if "分辨：群里某个账号是不是" in sysmsg:
        judge_calls.append(last)
        if "隔壁的抹香鲸" in last:
            out = {
                "bot": True,
                "mixed": False,
                "confidence": 72,
                "why": "秒接连发自称本小姐",
            }
        elif "小鲸" in last:
            out = {"bot": False, "mixed": False, "confidence": 55, "why": "口语像真人"}
        elif "账号名「阿木」" in last:  # K：把握 70（不到 75）—— 只能靠「行为分 ≥ 8」那条路标
            out = {"bot": True, "mixed": False, "confidence": 70, "why": "秒接连发"}
        else:
            out = {"bot": False, "mixed": False, "confidence": 85, "why": "真人"}
        text = json.dumps(out, ensure_ascii=False)
    else:
        chat_calls.append(last)
        # H：他说「晚安循环…」→ 回一模一样的一句（车轱辘话）
        text = "晚安啦 明天见 好梦哦" if "晚安循环" in last else f"收到第{len(chat_calls)}句"
    return web.json_response(
        {
            "id": "x",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "fake"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


async def models(request):
    return web.json_response(
        {"object": "list", "data": [{"id": "fake-model", "object": "model"}]}
    )


# ───────────── 假 QQ 端（NapCat 反向 WebSocket） ─────────────
class FakeQQ:
    def __init__(self):
        self.ws = None
        self.mid = 1000
        self.sent = []  # (群, 文字, 时间)
        self.store = {}  # message_id -> 消息
        self.names = {
            SELF: "大肥鱼",
            ADMIN: "管理员",
            HUMAN: "老王",
            WHALE: "隔壁的抹香鲸",
            KUN: "小鲸",
            OTHER: "路人",
            CMDBOT: "阿甲",
            BOT_A: "阿乙",
            BOT_B: "阿丙",
            BOT_C: "阿丁",
            LOOPER: "阿循",
            BOTCARD: "阿珍",
            BOTNICK: "小橙（Bot）",
            ROBOFAN: "老周",
            SIGNBOT: "老李",
            TALKER: "阿木",
        }
        # v0.4.0：群名片跟昵称不一样的（没写的 = 名片就是昵称）；空字符串 = 没设名片
        self.cards = {BOTCARD: "阿珍 [bot]", BOTNICK: "", ROBOFAN: "robot 爱好者"}
        self.self_cards = dict(SELF_CARDS)  # 你的机器人在各个群的名片（J 会改）
        self.long_nick = {SIGNBOT: "自动回复 ［BOT］", SELF: SELF_SIGN}
        self.stranger = {}  # QQ -> 查了几次 get_stranger_info
        self.ops = []  # J：按先后记下 私聊 / 改名片 / 改签名
        self.new_sent = asyncio.Event()

    async def connect(self):
        for _ in range(120):
            try:
                self.ws = await websockets.connect(
                    f"ws://127.0.0.1:{WS_PORT}/ws/",
                    additional_headers={
                        "X-Self-ID": str(SELF),
                        "X-Client-Role": "Universal",
                    },
                )
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not self.ws:
            raise RuntimeError("连不上 AstrBot 的反向 WebSocket")
        asyncio.create_task(self.loop())
        await self.ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "self_id": SELF,
                    "time": int(time.time()),
                }
            )
        )

    async def loop(self):
        async for raw in self.ws:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if "action" not in d:
                continue
            act, p, echo = d["action"], d.get("params") or {}, d.get("echo")
            data = {}
            if act in ("send_group_msg", "send_msg"):
                self.mid += 1
                msg = p.get("message")
                text = (
                    "".join(
                        (s.get("data") or {}).get("text", "")
                        for s in msg
                        if isinstance(s, dict) and s.get("type") == "text"
                    )
                    if isinstance(msg, list)
                    else str(msg)
                )
                self.sent.append((int(p.get("group_id") or 0), text, time.time()))
                self.store[self.mid] = {
                    "message_id": self.mid,
                    "message_type": "group",
                    "group_id": p.get("group_id"),
                    "sender": {"user_id": SELF, "nickname": "大肥鱼"},
                    "user_id": SELF,
                    "self_id": SELF,
                    "time": int(time.time()),
                    "message": msg
                    if isinstance(msg, list)
                    else [{"type": "text", "data": {"text": text}}],
                }
                data = {"message_id": self.mid}
                self.new_sent.set()
            elif act == "get_group_member_info":
                uid, gid = int(p.get("user_id") or 0), int(p.get("group_id") or 0)
                data = {
                    "user_id": uid,
                    "group_id": gid,
                    "nickname": self.names.get(uid, str(uid)),
                    "card": self.self_cards.get(gid, "大肥鱼")
                    if uid == SELF
                    else self.card(uid),
                    "role": "member",
                }
            elif act == "get_stranger_info":
                uid = int(p.get("user_id") or 0)
                self.stranger[uid] = self.stranger.get(uid, 0) + 1
                data = {
                    "user_id": uid,
                    "nickname": self.names.get(uid, str(uid)),
                    "long_nick": self.long_nick.get(uid, ""),
                }
            elif act == "get_group_list":
                data = [
                    {"group_id": g, "group_name": f"群{g}"}
                    for g in (G1, G2, G3, G4, G5, G6, G7)
                ]
            elif act == "set_group_card":
                gid, uid = int(p.get("group_id") or 0), int(p.get("user_id") or 0)
                if uid == SELF:
                    self.self_cards[gid] = str(p.get("card") or "")
                self.ops.append(("card", gid, str(p.get("card") or ""), time.time()))
            elif act == "set_self_longnick":
                self.long_nick[SELF] = str(p.get("longNick") or "")
                self.ops.append(("longnick", self.long_nick[SELF], time.time()))
            elif act == "send_private_msg":
                self.mid += 1
                msg = p.get("message")
                text = (
                    "".join(
                        (s.get("data") or {}).get("text", "")
                        for s in msg
                        if isinstance(s, dict) and s.get("type") == "text"
                    )
                    if isinstance(msg, list)
                    else str(msg)
                )
                self.ops.append(("dm", int(p.get("user_id") or 0), text, time.time()))
                data = {"message_id": self.mid}
            elif act == "get_msg":
                data = self.store.get(int(p.get("message_id") or 0)) or {}
            elif act == "get_login_info":
                data = {"user_id": SELF, "nickname": "大肥鱼"}
            await self.ws.send(
                json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo})
            )

    def card(self, uid):
        return self.cards.get(uid, self.names.get(uid, ""))

    async def say(self, gid, uid, text, at_bot=False, quote=None, ats=()):
        self.mid += 1
        segs = []
        if quote:
            segs.append({"type": "reply", "data": {"id": str(quote)}})
        for a in ats:  # K：@ 别人（不是机器人）
            segs.append({"type": "at", "data": {"qq": str(a)}})
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF)}})
            segs.append({"type": "text", "data": {"text": " " + text}})
        else:
            segs.append({"type": "text", "data": {"text": (" " if ats else "") + text}})
        ev = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": self.mid,
            "group_id": gid,
            "user_id": uid,
            "self_id": SELF,
            "time": int(time.time()),
            "message": segs,
            "raw_message": text,
            "font": 0,
            "sender": {
                "user_id": uid,
                "nickname": self.names.get(uid, str(uid)),
                "card": self.card(uid),
                "role": "member",
            },
        }
        self.store[self.mid] = dict(ev)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return self.mid

    async def wait_reply(self, gid, since, timeout=8.0):
        # 机器忙的时候（2 核、好几个测试一起跑）回得慢：E2E_SLOW=2 把等的时间放宽一倍
        end = time.time() + timeout * SLOW
        while time.time() < end:
            got = [s for s in self.sent if s[0] == gid and s[2] >= since]
            if got:
                return got
            self.new_sent.clear()
            try:
                await asyncio.wait_for(
                    self.new_sent.wait(), max(0.05, end - time.time())
                )
            except asyncio.TimeoutError:
                pass
        return []


def setup_root():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT))
    subprocess.run(
        ["astrbot", "init", "-y"], cwd=ROOT, env=env, check=True, capture_output=True
    )
    shutil.copytree(
        PLUGIN_SRC,
        ROOT / "data" / "plugins" / PNAME,
        ignore=shutil.ignore_patterns("__pycache__", "dev", "*.pyc", ".git"),
    )
    # 先让 AstrBot 自己生成默认配置，再改
    code = (
        "from astrbot.core.config.astrbot_config import AstrBotConfig;"
        f"AstrBotConfig(config_path=r'{ROOT / 'data' / 'cmd_config.json'}')"
    )
    subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, check=True, capture_output=True
    )
    p = ROOT / "data" / "cmd_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    cfg["platform"] = [
        {
            "id": "napcat",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": WS_PORT,
            "ws_reverse_token": "",
        }
    ]
    cfg["provider"] = [
        {
            "id": "fake",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": ["sk-fake"],
            "api_base": f"http://127.0.0.1:{LLM_PORT}/v1",
            "model": "fake-model",
            "timeout": 30,
        }
    ]
    cfg["admins_id"] = [str(ADMIN)]
    # G：只有 G4 开 AstrBot 自带的主动回复（概率 1 = 每句没 @ 的都接），看插件拦不拦「回指令」的那几句
    cfg["provider_ltm_settings"]["active_reply"] = {
        "enable": True,
        "method": "possibility_reply",
        "possibility_reply": 1.0,
        "whitelist": [str(G4), str(G6)],  # I：G6 也开（看自报家门的号第一句就不接）
    }
    cfg["platform_settings"]["rate_limit"]["count"] = 1000
    cfg.setdefault("dashboard", {})["port"] = DASH_PORT
    cfg["dashboard"]["host"] = "127.0.0.1"
    cfg["dashboard"]["jwt_secret"] = "e2e-secret-0123456789abcdef0123456789"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # v0.4.0：这个插件的配置（只写要改的几项，别的 AstrBot 按默认补上）
    # J 一键挂标记默认关 → 打开、只改 G1~G3、签名也加；I 查签名的全局间隔调成 0（测试里一会儿就来好几个新号）
    pc = ROOT / "data" / "config"
    pc.mkdir(parents=True, exist_ok=True)
    (pc / f"{PNAME}_config.json").write_text(
        json.dumps(
            {
                "self_tag_enable": True,
                "self_tag_groups": [str(G1), str(G2), str(G3)],
                "self_tag_sign": True,
                "sign_check_gap_sec": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    # G：一个测试用的复读插件（市场上常见的那种：群里连着三句一样、不止一个人说 → 跟一句；只在 G4）
    rp = ROOT / "data" / "plugins" / "astrbot_plugin_e2e_repeat"
    rp.mkdir(parents=True, exist_ok=True)
    (rp / "metadata.yaml").write_text(
        "name: astrbot_plugin_e2e_repeat\ndisplay_name: e2e 复读\ndesc: 测试用\n"
        "version: v0.0.1\nauthor: e2e\nrepo: https://example.invalid/e2e\n",
        encoding="utf-8",
    )
    (rp / "main.py").write_text(REPEAT_PLUGIN, encoding="utf-8")


REPEAT_PLUGIN = f"""
import sys
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star


class E2ERepeat(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.last = []

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=sys.maxsize - 1)
    async def on_msg(self, event: AstrMessageEvent):
        if str(event.get_group_id()) != "{G4}":
            return
        t = (event.message_str or "").strip()
        self.last = (self.last + [(str(event.get_sender_id()), t)])[-3:]
        if t and len(self.last) == 3 and len({{x[1] for x in self.last}}) == 1 and len({{x[0] for x in self.last}}) >= 2:
            self.last = []
            yield event.plain_result(t)

    # 关键词插件常见的写法：不用叫醒 AstrBot、优先级默认（排在 AstrBot 主动回复后面）
    @filter.regex(r"^#签到$")
    async def sign(self, event: AstrMessageEvent):
        if str(event.get_group_id()) == "{G4}":
            yield event.plain_result("签到成功（e2e）")
"""


async def main():
    setup_root()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()

    env = dict(
        os.environ,
        ASTRBOT_ROOT=str(ROOT),
        DASHBOARD_PORT=str(DASH_PORT),
        PYTHONUNBUFFERED="1",
    )
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen(
        ["astrbot", "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    qq = FakeQQ()
    token = jwt.encode(
        {"username": "astrbot", "exp": int(time.time()) + 3600},
        "e2e-secret-0123456789abcdef0123456789",
        algorithm="HS256",
    )
    hdr = {"Authorization": f"Bearer {token}"}

    async def panel():
        """面板接口读到的状态（v0.4.0 的几段用它看名单、判断记录）"""
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/state",
                headers=hdr,
            ) as r:
                d = await r.json(content_type=None)
        return (d or {}).get("data") or {}

    def bot_of(st, uid):
        return next((b for b in st.get("bots", []) if b.get("uid") == str(uid)), None)

    try:
        await qq.connect()
        await asyncio.sleep(4)  # 等插件都加载完
        text = (ROOT / "astrbot_stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        check(
            PNAME in text and "Traceback" not in text,
            "AstrBot 起来了、插件加载了、没有报错",
            text[-1500:],
        )

        # ═════════ A 真人 ═════════
        print("\n[A 真人连着 @ 机器人]")
        n = 0
        for i in range(12):
            t0 = time.time()
            await qq.say(
                G1,
                HUMAN,
                ["今天吃啥", "你说呢", "哈哈好吧", "那明天呢", "我先去忙了", "回来了"][
                    i % 6
                ]
                + str(i),
                at_bot=True,
            )
            if await qq.wait_reply(G1, t0):
                n += 1
            await asyncio.sleep(1.2 + (i % 3))
        check(n == 12, f"真人连着 @ 了 12 次：回了 {n} 次（不刹）", n)
        check(
            chat_calls
            and not any(
                "# 对面是别人部署的 AI" in x or "# 你们在来回说同一套" in x
                for x in chat_calls
            ),
            "真人正常聊：给模型的请求里没有「别人部署的 AI」「来回说同一套」（v0.3.0）",
        )
        check(
            not any("老王" in x for x in judge_calls),
            "真人（跟机器人你一句我一句聊得快）没被叫模型看",
            [x[:200] for x in judge_calls],
        )

        # ═════════ B 抹香鲸 ═════════
        print("\n[B 自称本小姐的机器人]")
        lines = [
            ("叫谁孩子呢", "本小姐可不吃这套"),
            ("骂谁呢，嘴这么脏", "本小姐可是干净鱼"),
            ("<h>本小姐驾到</h>？", "被放出来怎么了"),
            ("杂鱼？你倒是把战绩拿出来比比", "真的，光会喊"),
            ("就这？骂都骂不利索", "本小姐还当你能说出点什么呢"),
        ]
        for a, b in lines:
            await qq.say(G2, WHALE, a)
            await asyncio.sleep(1.5)
            await qq.say(G2, WHALE, b)
            await asyncio.sleep(2)
        await asyncio.sleep(3)
        check(
            any("隔壁的抹香鲸" in x for x in judge_calls),
            "攒够几句以后插件让模型看了一次",
            len(judge_calls),
        )
        wb = [x for x in judge_calls if "隔壁的抹香鲸" in x]
        check(
            wb
            and "机器人框架的输出：报错/状态/标签 <h>" in wb[0]
            and "（隔 " in wb[0]
            and "就这？骂都骂不利索" in wb[0],
            "给模型看的：最近的话都在、<h> 那条标成「框架输出」",
            wb[0][:600] if wb else "",
        )
        n = 0
        for i in range(6):
            t0 = time.time()
            await qq.say(G2, WHALE, f"你倒是说话啊{i}", at_bot=True)
            if await qq.wait_reply(G2, t0, timeout=4):
                n += 1
        check(n == 3, f"认出来以后它 @ 了 6 次：只回了 {n} 次（10 分钟最多 3 次）", n)
        t0 = time.time()
        await qq.say(G2, WHALE, "本小姐自己跟自己说")
        await asyncio.sleep(2)
        check(not await qq.wait_reply(G2, t0, timeout=1), "它没 @ 机器人：不主动接")
        wc = [x for x in chat_calls if "你倒是说话啊" in x]
        check(
            wc
            and all(
                "# 对面是别人部署的 AI（系统认出来的 不是谁说的话）" in x
                and "「隔壁的抹香鲸」是别人部署的 AI" in x
                and "别捡它刚用过的词和句式" in x
                for x in wc
            ),
            "回认出来的 AI：每次都先提醒模型别学它的说法（v0.3.0）",
            wc[:1],
        )

        # ═════════ C 小鲸：刹车 ═════════
        print("\n[C 对聊刹车（模型说像真人 但停不下来）]")
        replies, rounds_after_brake = 0, 0
        braked_at = None
        for i in range(12):
            t0 = time.time()
            await qq.say(G3, KUN, f"嘿嘿第{i}轮", at_bot=True)
            got = await qq.wait_reply(G3, t0, timeout=4)
            if got:
                replies += 1
                await asyncio.sleep(0.8)
                await qq.say(
                    G3, KUN, f"本鲸鲸才不服{i}"
                )  # 机器人说完 1 秒就接 两条连发
                await asyncio.sleep(0.6)
            elif braked_at is None:
                braked_at = i
            else:
                rounds_after_brake += 1
        check(
            braked_at is not None and replies == 9,
            f"机器人一说完它就接两条：回了 {replies} 次（第一次不算秒接 + 8 轮秒接）就刹住了",
            (replies, braked_at),
        )
        check(rounds_after_brake >= 2, "刹住以后它再 @ 也不回", rounds_after_brake)
        check(any("小鲸" in x for x in judge_calls), "刹车的时候叫模型马上再看了一眼")

        # ═════════ D 管理员指令 ═════════
        print("\n[D 管理员指令]")
        t0 = time.time()
        await qq.say(G2, ADMIN, "/认ai 列表")
        got = await qq.wait_reply(G2, t0)
        check(
            got and "隔壁的抹香鲸" in got[-1][1], "/认ai 列表：列出认出来的抹香鲸", got
        )
        t0 = time.time()
        await qq.say(G2, OTHER, "/认ai 列表")
        got = await qq.wait_reply(G2, t0, timeout=3)
        check(not got or "权限" in got[-1][1], "不是管理员：没权限（不列出来）", got)
        t0 = time.time()
        await qq.say(G2, ADMIN, f"/认ai 真人 {WHALE}")
        got = await qq.wait_reply(G2, t0)
        check(got and "真人" in got[-1][1], "/认ai 真人 <QQ>：改成真人", got)
        t0 = time.time()
        await qq.say(G2, WHALE, "现在呢", at_bot=True)
        check(bool(await qq.wait_reply(G2, t0)), "改回真人以后它 @ 机器人又会回了")

        # ═════════ E 面板接口 ═════════
        print("\n[E 面板接口]")
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/state",
                headers=hdr,
            ) as r:
                d = await r.json(content_type=None)
            st = (d or {}).get("data") or {}
            check(
                r.status == 200
                and "verdicts" in st
                and any(v.get("uid") == str(WHALE) for v in st.get("verdicts", [])),
                "GET state：读到最近的判断",
                (r.status, str(d)[:400]),
            )
            check(
                any(h.get("uid") == str(WHALE) for h in st.get("humans", [])),
                "状态里抹香鲸在「记成真人」里",
                st.get("humans"),
            )
            async with s.post(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/action",
                headers=hdr,
                json={"op": "bot", "uid": str(WHALE)},
            ) as r:
                d = await r.json(content_type=None)
            st = (d or {}).get("data") or {}
            check(
                r.status == 200
                and any(b.get("uid") == str(WHALE) for b in st.get("bots", [])),
                "POST action：面板里标回 AI",
                str(d)[:300],
            )
            async with s.post(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/action",
                headers=hdr,
                json={"op": "release", "uid": str(KUN)},
            ) as r:
                d = await r.json(content_type=None)
            check(
                r.status == 200 and not (d.get("data") or {}).get("brakes"),
                "POST action：解除刹车",
                str(d)[:300],
            )
            async with s.get(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/state"
            ) as r:
                check(r.status in (401, 403), f"没登录读不到（{r.status}）")
            async with s.get(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/views/panel",
                headers=hdr,
            ) as r:
                d = await r.json(content_type=None)
            raw = json.dumps(d, ensure_ascii=False)
            url = None

            def _find(o):
                nonlocal url
                if isinstance(o, dict):
                    for k, v in o.items():
                        if isinstance(v, str) and "/_t/" in v and url is None:
                            url = v
                        _find(v)
                elif isinstance(o, list):
                    for v in o:
                        _find(v)

            _find(d)
            check(
                r.status == 200 and url,
                "面板页 panel 认出来了（插件页面里能打开）",
                raw[:500],
            )
            if url:
                full = (
                    url
                    if url.startswith("http")
                    else f"http://127.0.0.1:{DASH_PORT}{url}"
                )
                async with s.get(full, headers=hdr) as r2:
                    html = await r2.text()
                check(
                    r2.status == 200
                    and "认 AI + 对聊刹车" in html
                    and "bridge-sdk.js" in html,
                    "面板页 HTML 拿得到、AstrBot 自动插上了桥接脚本",
                    (r2.status, html[:300]),
                )
        t0 = time.time()
        await qq.say(G3, KUN, "解除了吗", at_bot=True)
        check(bool(await qq.wait_reply(G3, t0)), "解除刹车以后它 @ 机器人又会回了")

        # ═════════ G 机器人们在回指令 ═════════
        print("\n[G 机器人们在回指令：不跟、不当成在找机器人聊（v0.2.0）]")
        t0 = time.time()
        await qq.say(G4, HUMAN, "大家晚上好", at_bot=True)
        check(
            bool(await qq.wait_reply(G4, t0)),
            "（准备）真人 @ 机器人：回了（这个群有了对话 主动回复才接得上）",
        )
        await asyncio.sleep(1.5)
        t0 = time.time()
        await qq.say(G4, HUMAN, "今天天气还行")
        check(
            bool(await qq.wait_reply(G4, t0, timeout=8)),
            "（对照）没 @ 的普通话：AstrBot 主动回复照常接",
        )
        await asyncio.sleep(2)
        t0 = time.time()
        await qq.say(G4, CMDBOT, "/stop")
        got = await qq.wait_reply(G4, t0)
        check(
            got and "No running tasks" in got[0][1],
            "别家的机器人发了 /stop：AstrBot 自己照常回指令（不拦）",
            got,
        )
        await asyncio.sleep(0.3)
        t1 = time.time()
        for u in (BOT_A, BOT_B, BOT_C):
            await qq.say(G4, u, "管理命令仅管理员可用。")
            await asyncio.sleep(0.15)
        await qq.say(
            G4, BOT_A, "已经停下了"
        )  # 没有「指令 / 权限」这些词 但在 /stop 后 15 秒内
        got = await qq.wait_reply(G4, t1, timeout=8)
        check(
            not any("仅管理员" in x[1] for x in got),
            "三个机器人同一秒回「管理命令仅管理员可用。」：没跟着复读（复读插件那句被拦下）",
            got,
        )
        check(not got, "指令后 15 秒内这一阵：一句都没接（主动回复也不接）", got)
        text = (ROOT / "astrbot_stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        check(
            "没跟着复读" in text and "是在回指令 不接" in text,
            "日志写清楚了：复读那句是这个插件拦的、主动回复是这个插件拦的",
        )
        # ═════════ H 车轱辘话（v0.3.0） ═════════
        print("\n[H 车轱辘话（v0.3.0）]")
        n0, got = len(chat_calls), 0
        for i in range(8):
            t0 = time.time()
            await qq.say(G5, LOOPER, f"晚安循环 晚安啦 明天见 好梦{i}", at_bot=True)
            if await qq.wait_reply(G5, t0):
                got += 1
            await asyncio.sleep(4.2)  # 隔开一点：别让他看着像几秒连发的机器人
        hc = [x for x in chat_calls[n0:] if "晚安循环" in x]
        head = "# 你们在来回说同一套（系统算的 不是谁说的话）"
        idx = [i for i, x in enumerate(hc) if head in x]
        check(got == 8, f"真人跟机器人来回说同一套 8 轮：每轮都照常回（{got}）", got)
        check(
            len(hc) == 8
            and idx
            and idx[0] >= 3
            and idx == list(range(idx[0], 8))
            and not any("# 对面是别人部署的 AI" in x for x in hc),
            "来回几轮以后（不是一开始）每一轮都给模型状态「你们在来回说同一套」；对面是真人 → 不提「别人部署的 AI」",
            (idx, hc[-1][-700:] if hc else ""),
        )
        last = hc[-1] if hc else ""
        check(
            "你跟「阿循」这 " in last
            and "「明天见」（你 " in last
            and "已经是车轱辘话了" in last
            and "你自己定" in last,
            "状态里写着跟谁、哪几个说法、各几次；绕够了 → 已经是车轱辘话了（不写台词）",
            last[-600:],
        )
        text = (ROOT / "astrbot_stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        check(
            "车轱辘话：阿循" in text and "第 2 档" in text,
            "日志里记了一行（同一个人同一档 5 分钟一行）",
        )
        check(
            not any(f"阿循({LOOPER})" in x and "刹" in x for x in text.splitlines()),
            "真人（行为不像机器人）：没刹车",
        )

        # ═════════ I 自报家门（v0.4.0） ═════════
        print("\n[I 自报家门（v0.4.0）]")
        t0 = time.time()
        await qq.say(G6, HUMAN, "大家晚上好", at_bot=True)
        check(
            bool(await qq.wait_reply(G6, t0)),
            "（准备）真人 @ 机器人：回了（这个群开着主动回复）",
        )
        await asyncio.sleep(1.5)
        t0 = time.time()
        await qq.say(G6, HUMAN, "今天天气还行")
        check(
            bool(await qq.wait_reply(G6, t0, timeout=8)),
            "（对照）真人没 @ 的话：主动回复照常接",
        )
        await asyncio.sleep(1.5)
        nj, nc = len(judge_calls), len(chat_calls)
        t0 = time.time()
        await qq.say(G6, BOTCARD, "大家好呀 我是新来的")
        got = await qq.wait_reply(G6, t0, timeout=6)
        st = await panel()
        b = bot_of(st, BOTCARD)
        check(
            b
            and str(b.get("why", "")).startswith("自报家门（群名片）")
            and b.get("src") == "self_card",
            "群名片「阿珍 [bot]」：第一句就认出来了（来源：自报家门（群名片））",
            b or [x.get("uid") for x in st.get("bots", [])],
        )
        check(not got, "它第一句没 @ 机器人：主动回复不接（已经当它是别人家的 AI）", got)
        check(
            len(judge_calls) == nj and not any("阿珍" in x for x in judge_calls),
            "没调模型判它（不攒、不调模型）",
            judge_calls[nj:],
        )
        check(
            not any("新来的" in x for x in chat_calls[nc:]),
            "也没叫模型回它那句",
            chat_calls[nc:],
        )
        t0 = time.time()
        await qq.say(G6, BOTCARD, "你好呀", at_bot=True)
        check(
            bool(await qq.wait_reply(G6, t0)),
            "它 @ 机器人：照认出来的 AI 的规矩回（只有 @ / 引用才回）",
        )
        await qq.say(G6, BOTNICK, "我也来了")
        await asyncio.sleep(0.5)
        await qq.say(G6, ROBOFAN, "我是 robot 爱好者")
        for i in range(3):  # 签名里挂着的：说好几句也只查一次签名
            await asyncio.sleep(0.8)
            await qq.say(G6, SIGNBOT, f"签名第{i}句")
        await asyncio.sleep(2.5)
        st = await panel()
        b = bot_of(st, BOTNICK)
        check(
            b and str(b.get("why", "")).startswith("自报家门（昵称）") and "(Bot)" in b.get("why", ""),
            "昵称「小橙（Bot）」（全角括号、大写）：认出来了（来源：昵称）",
            b,
        )
        check(not bot_of(st, ROBOFAN), "群名片「robot 爱好者」（没用括号包着）：不认")
        b = bot_of(st, SIGNBOT)
        check(
            b
            and str(b.get("why", "")).startswith("自报家门（签名）")
            and b.get("src") == "self_sign",
            "签名「自动回复 ［BOT］」：后台查了签名就认出来了（来源：签名）",
            b,
        )
        check(
            qq.stranger.get(SIGNBOT) == 1 and qq.stranger.get(BOTCARD, 0) == 0,
            f"签名每个号一天只查一次（说了 3 句、查了 {qq.stranger.get(SIGNBOT)} 次）；名片已经认出来的不用查",
            qq.stranger,
        )
        check(
            (st.get("signs") or {}).get("today", 0) >= 2,
            "面板里有「今天查了几个号的签名」",
            st.get("signs"),
        )
        qq.cards[BOTCARD] = "阿珍"  # 把群名片里的标记摘了
        t0 = time.time()
        await qq.say(G6, BOTCARD, "我把标记摘了")
        got = await qq.wait_reply(G6, t0, timeout=5)
        st = await panel()
        b = bot_of(st, BOTCARD)
        check(
            b and b.get("tag_gone") and not got,
            "摘了标记：不自动撤（还当它是机器人，没 @ 照旧不接），面板上标「标记已摘」",
            (b, got),
        )
        t0 = time.time()
        await qq.say(G6, ADMIN, f"/认ai 真人 {BOTNICK}")
        await qq.wait_reply(G6, t0)
        await asyncio.sleep(1)
        await qq.say(G6, BOTNICK, "我昵称里还挂着标记呢")
        await asyncio.sleep(1.5)
        st = await panel()
        check(
            not bot_of(st, BOTNICK)
            and any(h.get("uid") == str(BOTNICK) for h in st.get("humans", [])),
            "管理员手动撤了（/认ai 真人）：昵称里还挂着标记也不再认（管理员说了算）",
            (bot_of(st, BOTNICK), st.get("humans")),
        )

        # ═════════ J 一键挂标记 / 摘标记（v0.4.0） ═════════
        print("\n[J 一键挂标记 / 摘标记（v0.4.0）]")
        k0 = len(qq.ops)
        t0 = time.time()
        await qq.say(G1, ADMIN, "/认AI 挂标记")
        got = await qq.wait_reply(G1, t0, timeout=20)
        ops = qq.ops[k0:]
        dms = [i for i, o in enumerate(ops) if o[0] == "dm" and o[1] == ADMIN]
        cards = [(o[1], o[2]) for o in ops if o[0] == "card"]
        first = next((i for i, o in enumerate(ops) if o[0] in ("card", "longnick")), None)
        dm = ops[dms[0]][2] if dms else ""
        check(
            dms
            and first is not None
            and dms[0] < first
            and str(G1) in dm
            and str(G2) in dm
            and str(G3) not in dm
            and dm.startswith("（系统提示）"),
            "改之前先私聊管理员：改哪几个群（系统口吻）",
            ops,
        )
        check(
            sorted(cards) == sorted([(G1, "大肥鱼 [bot]"), (G2, "大肥鱼 [bot]")]),
            "调了假 NapCat 的 set_group_card：名片后面加「 [bot]」、名片空着的用昵称加；已经带着的（G3）不重复加",
            cards,
        )
        check(
            ("longnick", f"{SELF_SIGN} [bot]") in [o[:2] for o in ops],
            "签名也加上了（set_self_longnick）",
            ops,
        )
        reply = got[-1][1] if got else ""
        check(
            "挂好了 2 个群" in reply
            and "私聊" in reply
            and not any(x in reply for x in (str(G1), str(G2), str(G3), "大肥鱼")),
            "在群里发的：群里只回个数、说明细私聊了（群号、名片不晒给群友）",
            got,
        )
        dm2 = [o[2] for o in ops if o[0] == "dm" and o[1] == ADMIN][1:]
        check(
            dm2
            and all(x in dm2[-1] for x in (str(G1), str(G2), "大肥鱼 [bot]", "摘标记"))
            and dm2[-1].startswith("（系统提示）"),
            "明细私聊给发指令的管理员：哪几个群、改成什么、怎么摘",
            dm2,
        )
        await asyncio.sleep(1)
        k1 = len(qq.ops)
        t0 = time.time()
        await qq.say(G1, ADMIN, "/认AI 挂标记")
        got = await qq.wait_reply(G1, t0, timeout=20)
        check(
            not qq.ops[k1:]
            and got
            and "已经带着" in got[-1][1]
            and str(G1) not in got[-1][1],
            "再发一次：都带着了，不重复改、也不再私聊（群里只说几个群已经带着）",
            (qq.ops[k1:], got),
        )
        await asyncio.sleep(1)
        k2 = len(qq.ops)
        t0 = time.time()
        await qq.say(G1, ADMIN, "/认AI 摘标记")
        got = await qq.wait_reply(G1, t0, timeout=20)
        check(
            qq.self_cards == SELF_CARDS and qq.long_nick.get(SELF) == SELF_SIGN,
            "摘标记：群名片、签名原样恢复（空着的恢复成空的；G3 本来就带着的没动）",
            (qq.self_cards, qq.long_nick.get(SELF), qq.ops[k2:]),
        )
        dm3 = [o[2] for o in qq.ops[k2:] if o[0] == "dm" and o[1] == ADMIN]
        check(
            got
            and "改回去了 2 个群" in got[-1][1]
            and str(G1) not in got[-1][1]
            and dm3
            and str(G1) in dm3[-1]
            and "改回去了" in dm3[-1],
            "摘标记也一样：群里只回个数，改回了哪几个群私聊",
            (got, dm3),
        )

        # ═════════ K 真人信号一票否决（v0.4.0） ═════════
        print("\n[K 真人信号一票否决（v0.4.0）]")
        for p in (20002, 20003, 20004):  # 先主动 @ 三个这阵子没说话的人
            await qq.say(G7, TALKER, "作业交了没", ats=[p])
            await asyncio.sleep(0.5)
        replies, braked_at = 0, None
        for i in range(12):
            t0 = time.time()
            await qq.say(G7, TALKER, f"嘿嘿第{i}轮", at_bot=True)
            got = await qq.wait_reply(G7, t0, timeout=4)
            if not got:
                braked_at = i
                break
            replies += 1
            await asyncio.sleep(0.8)
            await qq.say(G7, TALKER, f"哈哈你说得对{i}")  # 机器人说完 1 秒就接、两条连发
            await asyncio.sleep(0.6)
        await asyncio.sleep(2)
        st = await panel()
        text = (ROOT / "astrbot_stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        v = next(
            (x for x in st.get("verdicts", []) if x.get("uid") == str(TALKER)), {}
        )
        check(
            braked_at is not None,
            f"秒接连发停不下来：照旧刹车（回了 {replies} 次）—— 刹车不归一票否决管",
            (replies, braked_at),
        )
        check(
            any("账号名「阿木」" in x for x in judge_calls),
            "叫模型看了（假模型：是 AI 把握 70，不到 75）",
        )
        check(
            not bot_of(st, TALKER),
            "行为分够了，但他主动 @ 过 3 个不同的人：没自动标成 AI",
            v,
        )
        check(
            v and "主动 @ 过 3 个不同的人" in v.get("watch", ""),
            "面板里记着「观察」、写着是哪条真人信号",
            v,
        )
        check(
            "只记「观察」" in text and "最近有真人信号：主动 @ 过 3 个不同的人" in text,
            "日志写清楚了是哪条信号否决的",
        )
        check(
            "不对称分（影子模式 只记不判）：阿木" in text and v.get("asym_if"),
            "不对称分（影子模式）：判的时候日志记了一行，面板里有「要是算进去会怎样」",
            v,
        )
        check(
            "stats" in (st.get("asym") or {}) and "verdict" in st["asym"]["stats"],
            "面板里有「这个分在已知的机器人 / 真人上分得开不开」",
            st.get("asym"),
        )

        await asyncio.sleep(16)
        t0 = time.time()
        await qq.say(G4, HUMAN, "好了说正事 晚上吃啥")
        check(
            bool(await qq.wait_reply(G4, t0, timeout=8)),
            "指令过去 15 秒以后：普通的话主动回复照常接（没拦过头）",
        )
        await asyncio.sleep(1.5)
        t0 = time.time()
        await qq.say(G4, HUMAN, "#签到")
        got = await qq.wait_reply(G4, t0, timeout=8)
        await asyncio.sleep(1.5)
        got = [x for x in qq.sent if x[0] == G4 and x[2] >= t0]
        check(
            any("签到成功" in x[1] for x in got),
            "没叫醒 AstrBot 的指令（#签到）：别的插件照常处理（插件不拦指令本身）",
            got,
        )
        n0 = len(judge_calls)
        t0 = time.time()
        await qq.say(G4, ADMIN, f"/认ai 看看 {BOT_A}")
        await qq.wait_reply(G4, t0, timeout=10)
        t0 = time.time()
        await qq.say(G4, ADMIN, f"/认ai 看看 {CMDBOT}")
        await qq.wait_reply(G4, t0, timeout=10)
        await asyncio.sleep(1)
        ba = next((x for x in judge_calls[n0:] if f"账号名「{BOT_A}」" in x), "")
        cb = next((x for x in judge_calls[n0:] if f"账号名「{CMDBOT}」" in x), "")
        check(
            "秒内就接上 0 次" in ba,
            "让模型看回指令的机器人：机器人回完指令它同一秒也回 —— 不算秒接",
            ba[-500:],
        )
        check(
            ba and "No running tasks" not in ba and "No running tasks" not in cb,
            "AstrBot 回的指令不算「你的机器人说的话」（来回里没有它）",
            (ba[-600:], cb[-300:]),
        )
        check(
            "@它 / 引用它 0 次" in cb,
            "发「/stop」的那个号：发指令不算 @ 你的机器人聊天",
            cb[-400:],
        )

        state = (
            json.loads(
                (ROOT / "data" / "plugin_data" / PNAME / "state.json").read_text(
                    encoding="utf-8"
                )
            )
            if (ROOT / "data" / "plugin_data" / PNAME / "state.json").exists()
            else {}
        )
        await asyncio.sleep(16)
        state = json.loads(
            (ROOT / "data" / "plugin_data" / PNAME / "state.json").read_text(
                encoding="utf-8"
            )
        )
        check(
            str(WHALE) in (state.get("bots") or {}),
            "名单存到了 data/plugin_data 里（重启还在）",
            list(state),
        )
        text = (ROOT / "astrbot_stdout.log").read_text(
            encoding="utf-8", errors="replace"
        )
        bad_tb = real_tracebacks(text)
        check(
            not bad_tb,
            "整个过程 AstrBot 日志里没有 Traceback（AstrBot 主动回复被拦时它自己打的 GeneratorExit 不算）",
            bad_tb[:1] or "",
        )

        print("\n[F 重启以后名单还在]")
        proc.terminate()
        proc.wait(15)
        await qq.ws.close()
        proc = subprocess.Popen(
            ["astrbot", "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT
        )
        qq2 = FakeQQ()
        await qq2.connect()
        await asyncio.sleep(4)
        t0 = time.time()
        await qq2.say(G2, WHALE, "本小姐又来了 你在吗", at_bot=False)
        await asyncio.sleep(1.5)
        check(
            not await qq2.wait_reply(G2, t0, timeout=1),
            "重启以后：抹香鲸还在名单里（没 @ 不接）",
        )
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}/state",
                headers=hdr,
            ) as r:
                d = await r.json(content_type=None)
        check(
            any(
                b.get("uid") == str(WHALE)
                for b in ((d or {}).get("data") or {}).get("bots", [])
            ),
            "重启以后：面板里名单还在",
        )
        check(
            any(
                b.get("uid") == str(BOTCARD) and b.get("src") == "self_card"
                for b in ((d or {}).get("data") or {}).get("bots", [])
            ),
            "重启以后：自报家门认出来的也还在（记着来源）",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
        await runner.cleanup()
        log.close()

    ok = sum(1 for c, _ in RES if c)
    bad = [m for c, m in RES if not c]
    print("\n" + "=" * 60)
    print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
    for m in bad:
        print("    -", m)
    print("=" * 60)
    if KEEP or bad:
        print("AstrBot 目录留着：", ROOT)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(1 if bad else 0)


asyncio.run(main())
