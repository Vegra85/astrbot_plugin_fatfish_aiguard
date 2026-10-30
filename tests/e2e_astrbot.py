#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试：真的 AstrBot（4.29.3）+ 这个插件 + 假 QQ 端（假 NapCat 反向 WebSocket）+ 假模型（OpenAI 兼容）
  A 真人老王连着 @ 机器人 12 次（每次隔几秒、一次一句）→ 每次都回、不刹、不叫模型看
  B 「隔壁的座头鲸」（自称本小姐、漏 <h> 标签、两条连发）：先在群里自己说几句 → 插件让模型看（假模型：AI 把握 72）→ 标上；
    之后它 @ 机器人 6 次 → 10 分钟里只回 3 次
  C 「小鲸」（假模型说像真人 55）：@ 机器人、机器人一说完 1 秒就接两条 → 第 8 轮刹住 → 后面不回
  D 管理员指令：/认ai 列表、/认ai 真人 <QQ>（改回真人以后座头鲸 @ 又会回了）；不是管理员 → 没权限
  E 面板接口：/api/v1/plugins/extensions/<插件>/state 能读到；/action 能改
用法：python tests/e2e_astrbot.py [插件目录 默认上一层] [--keep]
  需要装了 astrbot（测过 4.29.3）的 Python，astrbot 命令在 PATH 里；另外要 aiohttp、pyjwt、websockets（装 astrbot 时都会带上）
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
PNAME = PLUGIN_SRC.name
SELF, ADMIN, HUMAN, WHALE, KUN, OTHER = 99999, 10001, 30001, 40001, 40002, 50005
G1, G2, G3 = 7001, 7002, 7003
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
        if "隔壁的座头鲸" in last:
            out = {
                "bot": True,
                "mixed": False,
                "confidence": 72,
                "why": "秒接连发自称本小姐",
            }
        elif "小鲸" in last:
            out = {"bot": False, "mixed": False, "confidence": 55, "why": "口语像真人"}
        else:
            out = {"bot": False, "mixed": False, "confidence": 85, "why": "真人"}
        text = json.dumps(out, ensure_ascii=False)
    else:
        chat_calls.append(last)
        text = f"收到第{len(chat_calls)}句"
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
            WHALE: "隔壁的座头鲸",
            KUN: "小鲸",
            OTHER: "路人",
        }
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
                uid = int(p.get("user_id") or 0)
                data = {
                    "user_id": uid,
                    "nickname": self.names.get(uid, str(uid)),
                    "card": self.names.get(uid, ""),
                    "role": "member",
                }
            elif act == "get_stranger_info":
                uid = int(p.get("user_id") or 0)
                data = {"user_id": uid, "nickname": self.names.get(uid, str(uid))}
            elif act == "get_msg":
                data = self.store.get(int(p.get("message_id") or 0)) or {}
            elif act == "get_login_info":
                data = {"user_id": SELF, "nickname": "大肥鱼"}
            await self.ws.send(
                json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo})
            )

    async def say(self, gid, uid, text, at_bot=False, quote=None):
        self.mid += 1
        segs = []
        if quote:
            segs.append({"type": "reply", "data": {"id": str(quote)}})
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF)}})
            segs.append({"type": "text", "data": {"text": " " + text}})
        else:
            segs.append({"type": "text", "data": {"text": text}})
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
                "card": self.names.get(uid, ""),
                "role": "member",
            },
        }
        self.store[self.mid] = dict(ev)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return self.mid

    async def wait_reply(self, gid, since, timeout=8.0):
        end = time.time() + timeout
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
    cfg["platform_settings"]["rate_limit"]["count"] = 1000
    cfg.setdefault("dashboard", {})["port"] = DASH_PORT
    cfg["dashboard"]["host"] = "127.0.0.1"
    cfg["dashboard"]["jwt_secret"] = "e2e-secret-0123456789abcdef0123456789"
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


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
            not any("老王" in x for x in judge_calls),
            "真人（跟机器人你一句我一句聊得快）没被叫模型看",
            [x[:200] for x in judge_calls],
        )

        # ═════════ B 座头鲸 ═════════
        print("\n[B 自称本小姐的机器人]")
        lines = [
            ("叫谁孩子呢", "本小姐可不吃这套"),
            ("骂谁呢，嘴这么脏", "本小姐可是干净鱼"),
            ("<h>这不是本小姐吗</h>？", "被放出来怎么了"),
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
            any("隔壁的座头鲸" in x for x in judge_calls),
            "攒够几句以后插件让模型看了一次",
            len(judge_calls),
        )
        wb = [x for x in judge_calls if "隔壁的座头鲸" in x]
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
            got and "隔壁的座头鲸" in got[-1][1], "/认ai 列表：列出认出来的座头鲸", got
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
        token = jwt.encode(
            {"username": "astrbot", "exp": int(time.time()) + 3600},
            "e2e-secret-0123456789abcdef0123456789",
            algorithm="HS256",
        )
        hdr = {"Authorization": f"Bearer {token}"}
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
                "状态里座头鲸在「记成真人」里",
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
        check(
            "Traceback" not in text,
            "整个过程 AstrBot 日志里没有 Traceback",
            text[text.find("Traceback") - 300 :][:1500],
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
            "重启以后：座头鲸还在名单里（没 @ 不接）",
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
