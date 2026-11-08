#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自报家门（v0.4.0）单独测（不用装 AstrBot）：python tests/test_selfreport.py [插件目录 默认上一层]
  1 标记的各种写法、反例（名字里只是带 bot 字母的不算）
  2 查签名的限速和缓存（每个号一天一次、全局间隔、每天上限、查失败也算、重启还记得）
  3 一键挂 / 摘标记：改之前先说一声、原来的名片存下来、已经带着的不重复加、摘的时候原样改回去、后来被人改过的不动"""

import asyncio
import copy
import importlib.util
import sys
import time
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


try:
    spec = importlib.util.spec_from_file_location("selfreport", d / "core" / "selfreport.py")
    sr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sr)
except Exception as e:  # 老版本没有这个文件：算没过
    check(False, "有 core/selfreport.py（自报家门）", e)
    done()

print("[1 标记的写法]")
rx = sr.tag_rx()
yes = [
    "[bot]",
    "[BOT]",
    "［bot］",
    "［ＢＯＴ］",
    "【bot】",
    "(bot)",
    "（Bot）",
    "[ bot ]",
    "（　BOT　）",
    "阿珍 [bot]",
    "【Bot】小橙",
    "老王的机器人(bot)",
    "[bot】",  # 半角全角混着打错了也认
]
miss = [x for x in yes if not sr.find_tag(x, rx)]
check(not miss, f"默认的 [bot]：大小写、半角 / 全角括号、【】、括号里有空格都认（{len(yes)} 种）", miss)
no = [
    "robot 爱好者",
    "abbott",
    "(robot)",
    "[robot]",
    "bot",
    "Bot 小橙",
    "botany",
    "[bot1]",
    "[b o t]",
    "[ai]",
    "",
    None,
]
hit = [x for x in no if sr.find_tag(x, rx)]
check(not hit, "名字里只是带 bot 字母（robot 爱好者 / abbott / [robot]）、没用括号包着的：不算", hit)
check(sr.find_tag("小橙 ［ＢＯＴ］", rx) == "[BOT]", "认出来的那一段统一成半角（面板上看着整齐）", sr.find_tag("小橙 ［ＢＯＴ］", rx))
rx2 = sr.tag_rx(["[bot]", "[AI]", "机器人", " "])
check(
    all(sr.find_tag(x, rx2) for x in ("（ai）", "【机器人】", "[AI]", "(BOT)"))
    and not any(sr.find_tag(x, rx2) for x in ("AI 爱好者", "[AIR]", "机器人", "[机器人们]")),
    "自己加的写法：按括号里的字认（写「机器人」也只认括号包着的）",
)
check(
    sr.tag_list("[bot], [AI]，【机器人】\n[my bot]") == ["[bot]", "[AI]", "【机器人】", "[my bot]"]
    and sr.find_tag("小橙 [My  Bot]", sr.tag_rx("[my bot]")),
    "配置是一串字也行（按逗号、换行拆，不按空格拆：「[my bot]」中间的空格留着）",
)
check(sr.tag_rx([]) is None and sr.find_tag("[bot]", sr.tag_rx([])) == "", "写法清空 = 不认")
check(
    sr.scan_names("阿珍 [bot]", "阿珍", rx) == ("群名片", "[bot]")
    and sr.scan_names("", "小橙（Bot）", rx) == ("昵称", "(Bot)")
    and sr.scan_names("robot 爱好者", "abbott", rx) == ("", ""),
    "群名片 / 昵称分开看，记下是从哪看到的",
)
check(
    sr.sign_of({"user_id": 30003, "long_nick": "自动回复 ［BOT］"}) == "自动回复 ［BOT］"
    and sr.sign_of({"longNick": "早睡早起"}) == "早睡早起"
    and sr.sign_of({"nickname": "老李"}) == ""
    and sr.sign_of(None) == "",
    "签名：NapCat 的 long_nick（没有就当空的）",
)

print("\n[1b 摘了标记：不撤，只记「标记已摘」]")
su = getattr(sr, "seen_update", None)
if su is None:
    check(False, "有 seen_update（看自报家门的标记还在不在）")
else:
    import inspect

    GA, GB = "qq:7001", "qq:7002"
    by_group = "gk" in inspect.signature(su).parameters

    def su_(rec, t, **kw):
        if not by_group:  # 老代码：名片不按群记（gk 会被当成一种标记吞进去）—— 算没过
            return "名片不按群记"
        return su(rec, t, **kw)

    rec = {"src": "self_card", "seen": {"card": {GA: True}}}
    r = [su_(rec, 1000, gk=GA, card="", nick=""), rec.get("tag_gone"), su_(rec, 1010, gk=GA, card="", nick="")]
    check(r == ["gone", 1000, None], "群名片里的标记没了 → 记一次「标记已摘」（不重复记），记录还在（不撤）", (r, rec))
    r = [su_(rec, 1020, gk=GA, card="[bot]"), "tag_gone" in rec]
    check(r == ["back", False], "又挂上了 → 去掉「标记已摘」", (r, rec))
    # 11-08 复查：群名片是每个群一个 —— 只在 A 群挂了标记的号在 A、B 两个群轮流说话，以前「已摘 / 又挂上」来回翻
    rec = {"src": "self_card", "seen": {"card": {GA: True}}}
    r = []
    for i in range(3):
        r.append(su_(rec, 2000 + i * 10, gk=GB, card="", nick=""))
        r.append(su_(rec, 2005 + i * 10, gk=GA, card="[bot]", nick=""))
    check(r == [None] * 6 and "tag_gone" not in rec, "只在 A 群名片挂了标记、在 A / B 群轮流说话：不来回翻「已摘 / 又挂上」", (r, rec))
    check(su_(rec, 3000, gk=GA, card="", nick="") == "gone", "A 群的名片也摘了：才算摘", rec)
    rec = {"src": "self_sign", "seen": {"sign": True}}
    r = [su_(rec, 1000, gk=GA, card="", nick=""), su_(rec, 2000, sign="")]
    check(r == [None, "gone"], "签名认出来的：名片、昵称里本来就没有不算摘；签名再查一次没了才算", (r, rec))
    rec = {"src": "self_card", "seen": {"card": {GA: True}}}
    check(su_(rec, 1000, gk=GA, card="", nick="(BOT)") is None and "tag_gone" not in rec, "名片摘了、昵称还挂着：不算摘", rec)

print("\n[2 查签名的限速和缓存]")
t0 = time.mktime((2026, 11, 8, 10, 0, 0, 0, 0, -1))  # 当地时间上午 10 点：+1 天正好跨一次日子
st = {}
S = sr.SignCache(st)
r = [
    S.due("30003", t0, gap=30, daily_max=3),
]
S.start("30003", t0)
r += [
    S.due("30003", t0 + 1, gap=30, daily_max=3),  # 正在查
    S.due("20002", t0 + 10, gap=30, daily_max=3),  # 离上一次查不到 30 秒（全局限速）
    S.due("20002", t0 + 31, gap=30, daily_max=3),
]
check(r == [True, False, False, True], "全局限速：查完一个，30 秒内谁都不查；查着的那个不重复查", r)
S.start("20002", t0 + 31)
S.done("20002", t0 + 32, "[bot]")
S.done("30003", t0 + 2, "")
check(
    S.peek("20002", t0 + 40) == "[bot]" and S.peek("30003", t0 + 40) == "" and S.peek("40004", t0) is None,
    "查过的记着：有标记 / 没标记 / 没查过 分得清",
)
r = [
    S.due("30003", t0 + 23 * 3600, gap=30, daily_max=99),
    S.due("30003", t0 + 24 * 3600 + 5, gap=30, daily_max=99),
]
check(r == [False, True], "同一个号一天最多查一次（23 小时后不查、24 小时后可以）", r)
check(S.peek("20002", t0 + 24 * 3600 + 60) is None, "过了一天：记的结果不算数了（要重查）")
S.start("40004", t0 + 100)
r = [S.due("50005", t0 + 200, gap=30, daily_max=3), S.today(t0 + 200)]
check(r == [False, 3], "每天最多查几个号：今天查够 3 个就不查了", r)
check(S.due("50005", t0 + 24 * 3600 + 200, gap=30, daily_max=3), "第二天重新算")
S.failed("40004", t0 + 101, "TimeoutError()")
check(
    not S.due("40004", t0 + 3600, gap=0, daily_max=99) and S.peek("40004", t0 + 3600) is None,
    "查失败也算查过（接口挂着别一直调），也不当成「没标记」",
)
S2 = sr.SignCache(st)  # 重启：存档还是那份
check(not S2.due("30003", t0 + 3600, gap=0, daily_max=99) and S2.peek("20002", t0 + 60) == "[bot]", "重启以后还记得谁查过（不用全群重查）")
S2.prune(t0 + 3 * 24 * 3600)
check(not st["users"], "两天前的记录会扔掉（存档别越攒越大）", st["users"])


print("\n[3 一键挂 / 摘标记]")
SELF = "99999"


class FakeApi:
    """假 NapCat：get_group_list / get_group_member_info / set_group_card / get_stranger_info / set_self_longnick"""

    def __init__(self, cards, nick="大肥鱼", sign="今天也要加油", fail=()):
        self.cards = dict(cards)
        self.nick, self.sign, self.fail = nick, sign, set(fail)
        self.calls = []

    async def __call__(self, action, **kw):
        self.calls.append((action, kw))
        if action == "get_group_list":
            return [{"group_id": int(g), "group_name": f"群{g}"} for g in self.cards]
        if action == "get_group_member_info":
            return {"user_id": kw["user_id"], "card": self.cards[str(kw["group_id"])], "nickname": self.nick}
        if action == "set_group_card":
            g = str(kw["group_id"])
            if g in self.fail:
                raise RuntimeError("设置群名片未生效")
            self.cards[g] = kw["card"]
            return None
        if action == "get_stranger_info":
            return {"user_id": kw["user_id"], "nickname": self.nick, "long_nick": self.sign}
        if action == "set_self_longnick":
            self.sign = kw["longNick"]
            return None
        raise RuntimeError(f"假 NapCat 不认识 {action}")


def run(c):
    return asyncio.run(c)


orig = {"7001": "大肥鱼", "7002": "", "7003": "大肥鱼 [bot]", "7004": "阿木家的【BOT】"}
api = FakeApi(orig)
saved = {}
told = []


async def notify(gs, sign_too):
    told.append((list(gs), sign_too))
    api.calls.append(("notify", {}))


rep = run(sr.put_tags(api, SELF, [], rx, saved, sign=True, notify=notify, gap=0, now=t0))
acts = [a for a, _ in api.calls]
first_set = acts.index("set_group_card") if "set_group_card" in acts else -1
check(
    told == [(["7001", "7002"], True)] and 0 <= acts.index("notify") < first_set,
    "改之前先说一声：要改哪几个群、签名改不改（在第一次改名片之前）",
    (told, acts),
)
check(
    api.cards == {"7001": "大肥鱼 [bot]", "7002": "大肥鱼 [bot]", "7003": "大肥鱼 [bot]", "7004": "阿木家的【BOT】"},
    "群名片后面加上「 [bot]」；名片空着的用昵称加（群里显示的就是昵称）；已经带着标记的（哪种括号都算）不重复加",
    api.cards,
)
check(
    saved.get("groups", {}).get("7001", {}).get("old") == "大肥鱼"
    and saved["groups"].get("7002", {}).get("old") == ""
    and "7003" not in saved["groups"]
    and "7004" not in saved["groups"]
    and saved["groups"]["7001"]["ok"],
    "原来的名片存下来了（空着的存成空的）；没改的群不存",
    saved,
)
check(
    api.sign == "今天也要加油 [bot]" and saved.get("sign", {}).get("old") == "今天也要加油" and rep["sign"] == "done",
    "签名也加上了，原来的签名存下来了",
    (api.sign, saved.get("sign")),
)
check(
    [g for g, _ in rep["done"]] == ["7001", "7002"] and rep["had"] == ["7003", "7004"] and not rep["fail"],
    "回报：改了哪几个、哪几个本来就带着",
    rep,
)
n0 = len(api.calls)
told.clear()
rep2 = run(sr.put_tags(api, SELF, [], rx, saved, sign=True, notify=notify, gap=0, now=t0 + 60))
check(
    not [a for a, _ in api.calls[n0:] if a in ("set_group_card", "set_self_longnick")]
    and not told
    and saved["groups"]["7001"]["old"] == "大肥鱼",
    "再挂一次：都带着了，什么都不改、不打扰管理员；存的原名片不被冲掉",
    (api.calls[n0:], rep2),
)
rep3 = run(sr.take_tags(api, SELF, saved, gap=0))
check(
    api.cards == orig and api.sign == "今天也要加油" and not saved.get("groups") and "sign" not in saved,
    "摘标记：群名片、签名原样改回去（空着的改回空的）",
    (api.cards, api.sign, saved),
)
check(sorted(g for g, _ in rep3["done"]) == ["7001", "7002"] and rep3["sign"] == "done", "回报：改回了哪几个", rep3)

# 挂上以后有人手动改了名片 → 摘的时候不动它（别把人家后来改的冲掉）
api = FakeApi({"7001": "大肥鱼", "7002": "小橙"})
saved = {}
run(sr.put_tags(api, SELF, ["7001", "7002"], rx, saved, gap=0, now=t0))
check(
    not [a for a, _ in api.calls if a == "get_group_list"] and api.cards == {"7001": "大肥鱼 [bot]", "7002": "小橙 [bot]"},
    "配了群：只改这几个（不去查所有群）",
    api.calls,
)
api.cards["7001"] = "大肥鱼二号 [bot]"
rep = run(sr.take_tags(api, SELF, saved, gap=0))
check(
    api.cards == {"7001": "大肥鱼二号 [bot]", "7002": "小橙"}
    and rep["changed"] == [("7001", "大肥鱼二号 [bot]", "大肥鱼")],
    "挂上以后名片又被人改过：摘的时候不动它，告诉管理员原来是什么",
    rep,
)

# 改的时候有的群没改成
api = FakeApi({"7001": "大肥鱼", "7002": "大肥鱼"}, fail={"7002"})
saved = {}
rep = run(sr.put_tags(api, SELF, [], rx, saved, gap=0, now=t0))
check(
    [g for g, _ in rep["fail"]] == ["7002"] and saved["groups"]["7002"]["ok"] is False and api.cards["7002"] == "大肥鱼",
    "没改成的群：回报里写着；记录标成没改成",
    (rep, saved),
)
api.fail.clear()
rep = run(sr.take_tags(api, SELF, saved, gap=0))
check(
    api.cards == {"7001": "大肥鱼", "7002": "大肥鱼"} and not rep["changed"] and not saved["groups"],
    "摘的时候：没改成的那个本来就是原样，不动、也不报「后来改过」",
    rep,
)

# 接口出错：挂的时候读不到名片的群跳过
class Boom(FakeApi):
    async def __call__(self, action, **kw):
        if action == "get_group_member_info" and str(kw.get("group_id")) == "7002":
            raise RuntimeError("群(7002)成员不存在")
        return await super().__call__(action, **kw)


api = Boom({"7001": "大肥鱼", "7002": "大肥鱼"})
saved = {}
rep = run(sr.put_tags(api, SELF, [], rx, saved, gap=0, now=t0))
check(
    api.cards == {"7001": "大肥鱼 [bot]", "7002": "大肥鱼"} and [g for g, _ in rep["fail"]] == ["7002"] and "7002" not in saved["groups"],
    "读不到某个群的名片：那个群不改、不存，别的群照改",
    rep,
)

# 两个群之间歇一下（防 QQ 风控）
slept = []


async def fake_sleep(s):
    slept.append(s)


api = FakeApi({"7001": "a", "7002": "b", "7003": "c"})
run(sr.put_tags(api, SELF, [], rx, {}, gap=1.0, now=t0, sleep=fake_sleep))
check(slept == [1.0, 1.0], "一个群一个群地改，中间歇一下", slept)


def put(*a, **k):
    try:
        return run(sr.put_tags(*a, **k))
    except TypeError as e:  # 老代码没有这些参数：算没过
        return {"done": [], "had": [], "fail": [("?", f"出错 {e}")], "sign": "", "left": -1}


print("\n[3b 改每个群之前先存盘（11-08 复查：以前整趟跑完才存）]")
disk = []  # 每次「存盘」时硬盘上的样子


class Watch(FakeApi):
    async def __call__(self, action, **kw):
        if action == "set_group_card":  # 改这个群之前：硬盘上得已经有它原来的名片
            g = str(kw["group_id"])
            on_disk = disk[-1]["groups"].get(g) if disk else None
            self.ok_before.append(bool(on_disk) and on_disk.get("old") == self.orig[g])
        if action == "set_self_longnick":
            self.ok_before.append(bool(disk) and (disk[-1].get("sign") or {}).get("old") == self.orig_sign)
        return await super().__call__(action, **kw)


api = Watch({"7001": "大肥鱼", "7002": "", "7003": "小橙"})
api.orig, api.orig_sign, api.ok_before = dict(api.cards), api.sign, []
saved = {}
rep = put(api, SELF, [], rx, saved, sign=True, gap=0, now=t0, persist=lambda: disk.append(copy.deepcopy(saved)))
check(
    api.ok_before == [True, True, True, True] and len(rep["done"]) == 3,
    "改每个群（和签名）之前，原来的名片 / 签名已经存到硬盘上了",
    (api.ok_before, rep),
)


def boom():
    raise OSError("磁盘满了")


api = FakeApi({"7001": "大肥鱼"})
saved = {}
rep = put(api, SELF, [], rx, saved, gap=0, now=t0, persist=boom)
check(
    api.cards == {"7001": "大肥鱼"} and not saved.get("groups") and [g for g, _ in rep["fail"]] == ["7001"],
    "原名片存不到硬盘上：这个群不改（不然出了事就找不回来）",
    (rep, saved),
)

print("\n[3c 没配群（= 所有群）：一次最多改 20 个（11-08 复查：以前没上限）]")
many = {str(7100 + i): f"大肥鱼{i}" for i in range(30)}
api = FakeApi(many)
saved = {}
told.clear()
rep = put(api, SELF, [], rx, saved, notify=notify, gap=0, now=t0)
changed1 = [g for g, _ in rep["done"]]
check(
    len(changed1) == 20 and changed1 == sorted(many)[:20] and rep.get("left") == 10 and rep.get("total") == 30,
    "机器人在 30 个群：这次只改前 20 个（按群号从小到大），说清楚还有 10 个没看",
    (len(changed1), rep.get("left"), rep.get("total")),
)
check(told and len(told[-1][0]) == 20, "改之前私聊说的也是这 20 个", told[-1:] if told else told)
rep = put(api, SELF, [], rx, saved, gap=0, now=t0 + 60)
check(
    len(rep["done"]) == 10 and len(rep["had"]) == 20 and rep.get("left") == 0
    and all(c.endswith(" [bot]") for c in api.cards.values()),
    "再发一次：前 20 个已经带着、接着改剩下的 10 个",
    (len(rep["done"]), len(rep["had"]), rep.get("left")),
)
api = FakeApi(dict(many))
rep = put(api, SELF, sorted(many), rx, {}, gap=0, now=t0)
check(len(rep["done"]) == 30 and not rep.get("left"), "配了群（管理员自己列的）：列了几个改几个，不设上限", len(rep["done"]))

print("\n[3d 挂 / 摘的结果：群里只回个数，群号和名片的明细私聊（11-08 复查）]")
trf = getattr(sr, "tag_report", None)
if trf is None:
    check(False, "有 tag_report（分开「群里回的」和「私聊的明细」）")
else:
    rep = {"done": [("7001", "大肥鱼 [bot]"), ("7002", "小橙 [bot]")], "had": ["7003"], "fail": [("7004", "设置群名片未生效")],
           "sign": "done", "total": 4, "left": 0}
    short, detail, has = trf(rep, on=True)
    check(
        not any(x in short for x in ("7001", "7002", "7003", "7004", "大肥鱼", "小橙")) and "2 个群" in short and has,
        "群里回的：只有个数，没有群号、名片",
        short,
    )
    check(
        all(x in detail for x in ("7001", "大肥鱼 [bot]", "7004", "摘标记")),
        "私聊的明细：群号、名片、没改成的、怎么摘都在",
        detail,
    )
    s2, d2, h2 = trf({"done": [], "had": ["7001", "7002"], "fail": [], "sign": "had"}, on=True)
    check(not h2 and "7001" not in s2 and "2 个群已经带着标记" in s2, "什么都没改（都带着）：不用私聊，群里也不报群号", s2)
    s3, d3, h3 = trf({"done": [("7001", "大肥鱼")], "changed": [("7002", "改过了 [bot]", "小橙")], "fail": [], "sign": ""}, on=False)
    check(h3 and "7002" not in s3 and "改过了" not in s3 and "7002" in d3 and "改回去了" in d3, "摘标记也一样：群里只回个数", (s3, d3))
    s4, d4, h4 = trf({"done": [("7120", "x [bot]")], "had": [], "fail": [], "sign": "", "total": 30, "left": 10}, on=True)
    check("10 个群这次没看" in s4 and "再发一次" in d4, "一次没改完：群里、明细都说还有几个没看、怎么接着挂", (s4, d4))

done()
