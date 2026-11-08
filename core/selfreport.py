# -*- coding: utf-8 -*-
"""
自报家门（v0.4.0，纯逻辑，不 import astrbot，单独能测）

作者定的约定：部署机器人的人把「[bot]」挂在机器人的群名片、QQ 昵称或 QQ 签名里。
别的机器人看到就知道它是机器人：不用攒几句再猜、不用花钱让模型看，两条鱼也不会互聊起来。

- 认的写法：括号包着的那几个字。大小写、半角 / 全角括号、【】都认，括号里可以有空格。
  名字里只是带 bot 这几个字母（「robot 爱好者」「abbott」）不算 —— 不包在括号里，太容易误伤。
- 签名要调 QQ 的接口查：每个号一天最多查一次、全局限速（两次之间至少隔几秒、每天最多查几个号），查过的记着。
  只记「有没有标记」，不存签名原文。
- 一键给自己挂上（put_tags / take_tags）：改之前把原来的群名片 / 签名存下来，摘的时候原样改回去。
"""

import asyncio
import re
import time
import unicodedata

SELF_TAG = "[bot]"  # 一键挂标记加的就是这个（作者定的约定，别人的机器人认的也是它）
DEFAULT_TAGS = (SELF_TAG,)
# NFKC 以后全角的［］（）已经变成半角；【】〔〖 没有半角写法，单列
_OPEN = "[(【〔〖"
_CLOSE = "])】〕〗"
SIGN_TTL = 24 * 3600  # 每个号一天最多查一次签名
ALL_MAX = 20  # 一键挂标记没配群（= 所有群）时一次最多改几个群：群多的号一口气改几十个群的名片，容易被 QQ 风控、也难收拾


def norm(s):
    """全角 → 半角（［ＢＯＴ］→ [BOT]、（）→ ()、全角空格 → 空格）：用户打字半角全角混着来，统一了才好认"""
    return unicodedata.normalize("NFKC", str(s or ""))


def inner(tag):
    """配置里的一种写法 → 括号里的字：「[bot]」「（Bot）」「bot」都是 bot。
    不管用户写的是哪种括号，认的时候哪种括号都认 —— 用户只用想「标记是哪几个字」"""
    t = norm(tag).strip()
    if len(t) >= 2 and t[0] in _OPEN and t[-1] in _CLOSE:
        t = t[1:-1]
    return re.sub(r"\s+", " ", t).strip().casefold()


def tag_list(v):
    """配置里的写法列表：list 照用；老配置 / 手填成一串字的，按逗号、换行拆（不按空格拆：「[my bot]」中间有空格）"""
    if isinstance(v, str):
        v = re.split(r"[,，\n]", v)
    return [str(x).strip() for x in v or () if str(x).strip()]


def tag_rx(tags=DEFAULT_TAGS):
    """写法列表 → 一个正则：任何一种括号 + 可以有空格 + 括号里的字（不分大小写）；列表空 → None（等于关掉）"""
    ins = sorted({inner(x) for x in tag_list(tags)} - {""}, key=len, reverse=True)
    if not ins:
        return None
    alt = "|".join(
        re.escape(x).replace("\\ ", r"\s+").replace(" ", r"\s+") for x in ins
    )
    return re.compile(
        "[" + re.escape(_OPEN) + r"]\s*(?:" + alt + r")\s*[" + re.escape(_CLOSE) + "]",
        re.I,
    )


def find_tag(text, rx):
    """这段字里有没有标记 → 认出来的那一段（统一成半角，比如「[BOT]」）；没有 → 空字符串"""
    if rx is None or not text:
        return ""
    m = rx.search(norm(text))
    return m.group(0) if m else ""


def scan_names(card, nick, rx):
    """群名片 / 昵称（每条消息都带着，不用另外查）→ (在哪, 标记)；都没有 → ("", "")"""
    for where, s in (("群名片", card), ("昵称", nick)):
        t = find_tag(s, rx)
        if t:
            return where, t
    return "", ""


def sign_of(info):
    """get_stranger_info 的返回 → 签名。NapCat 的字段是 long_nick（它把 longNick 也一起摊开在返回里）；
    别家 OneBot 实现可能叫 sign / signature —— 都试一下，没有就当空的"""
    if not isinstance(info, dict):
        return ""
    for k in ("long_nick", "longNick", "sign", "signature"):
        v = info.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


class SignCache:
    """查签名的限速和缓存：每个号一天最多查一次；全局两次之间至少 gap 秒、每天最多 daily_max 个号。
    签名要调 QQ 的接口，群里人多的时候一人一次也不少 —— 查太勤容易被 QQ 风控，所以宁可慢。
    store 是存档里的一个 dict：重启以后还记得谁今天查过（不然一重启全群重查一遍）"""

    def __init__(self, store=None, ttl=SIGN_TTL):
        self.s = store if isinstance(store, dict) else {}
        self.s.setdefault("users", {})
        self.ttl = float(ttl)

    @staticmethod
    def day(now):
        return time.strftime("%Y-%m-%d", time.localtime(now))

    def today(self, now):
        """今天查了几个号"""
        return int(self.s.get("n") or 0) if self.s.get("day") == self.day(now) else 0

    def due(self, uid, now, gap=30, daily_max=300):
        """现在该不该查他：一天内查过（查失败也算）→ 不查；离上一次查别人不到 gap 秒 / 今天查够数了 → 这次先不查
        （他下一句再说）"""
        u = self.s["users"].get(str(uid))
        if u and now - float(u.get("t") or 0) < self.ttl:
            return False
        if now - float(self.s.get("last") or 0) < float(gap):
            return False
        return self.today(now) < int(daily_max)

    def start(self, uid, now):
        """要查了：先记上（查的这一两秒里他又说一句，不会再查一次）"""
        d = self.day(now)
        if self.s.get("day") != d:
            self.s["day"], self.s["n"] = d, 0
        self.s["n"] = int(self.s.get("n") or 0) + 1
        self.s["last"] = float(now)
        self.s["users"][str(uid)] = {"t": float(now), "busy": 1}

    def done(self, uid, now, tag=""):
        self.s["users"][str(uid)] = {"t": float(now), "tag": str(tag or "")}

    def failed(self, uid, now, err=""):
        """查失败也算查过：接口挂着的时候别一个号一直调"""
        self.s["users"][str(uid)] = {"t": float(now), "err": str(err or "")[:80]}

    def peek(self, uid, now):
        """→ None：没查过 / 过期了 / 查失败；""：查过、没有标记；"[bot]" 这种：签名里有标记"""
        u = self.s["users"].get(str(uid))
        if not u or now - float(u.get("t") or 0) >= self.ttl or "tag" not in u:
            return None
        return str(u.get("tag") or "")

    def prune(self, now, keep=2 * SIGN_TTL):
        """两天前的记录扔掉：存档别越攒越大"""
        us = self.s["users"]
        for k in [k for k, u in us.items() if now - float(u.get("t") or 0) > keep]:
            us.pop(k, None)


def seen_update(rec, now, gk="", **chan):
    """自报家门认出来的号，这次看到的标记（card / nick / sign 各有没有）→ "gone"（刚摘：都没了）/ "back"（又挂上了）/ None（没变）。
    rec 是名单里它那条记录（会改）。摘了也不撤，只记 tag_gone：挂挂摘摘的时候认 / 不认跟着来回切更乱，撤不撤让管理员定。
    没看到的那一样（比如这次没查签名）保留上次看到的样子。
    群名片是每个群一个（只在 A 群挂了标记的号在 A、B 两个群轮流说话，不能来回算成摘了 / 又挂上 —— 11-08 复查），
    所以 card 按群记（gk 是这条消息的群）；昵称、签名全 QQ 一个，照旧一份。随便哪个群的名片还挂着就算没摘"""
    seen = rec.get("seen")
    if not isinstance(seen, dict):
        seen = rec["seen"] = {}
    cards = seen.get("card")
    if not isinstance(cards, dict):  # 不是按群记的（不知道是哪个群的）：扔掉重新看
        cards = seen["card"] = {}
    for k, v in chan.items():
        if k == "card":
            cards[str(gk)] = bool(v)
        else:
            seen[k] = bool(v)
    has = any(cards.values()) or any(
        bool(v) for k, v in seen.items() if k != "card"
    )
    if has and rec.pop("tag_gone", None):
        return "back"
    if not has and not rec.get("tag_gone"):
        rec["tag_gone"] = int(now)
        return "gone"
    return None


# ───────── 一键给自己挂标记 / 摘标记 ─────────
def with_tag(base, tag=SELF_TAG):
    """名字后面加「 [bot]」；本来是空的就只有「[bot]」"""
    b = str(base or "").rstrip()
    return f"{b} {tag}" if b else tag


async def _pause(sec, sleep):
    # 一个群接一个群地改、中间歇一下：短时间连着改很多群的名片容易被 QQ 风控
    if sec and float(sec) > 0:
        await sleep(float(sec))


def _err(e):
    return f"{e!r}"[:80]


def _gkey(g):
    g = str(g)
    return (0, int(g), g) if g.isdigit() else (1, 0, g)


def _persist(persist):
    """改之前先存盘（存不下来就别改：不然改到一半出事，原来的名片就找不回来了）→ 存下了没有"""
    if persist is None:
        return True
    try:
        persist()
        return True
    except Exception:
        return False


async def put_tags(
    call,
    self_id,
    gids,
    rx,
    saved,
    sign=False,
    notify=None,
    gap=1.0,
    now=None,
    sleep=asyncio.sleep,
    tag=SELF_TAG,
    persist=None,
    limit=None,
):
    """一键挂标记：在这些群把自己的群名片后面加上「 [bot]」（sign=True 签名也加）。
    call(动作, **参数) → 接口返回的 data（出错就抛）；
    gids 空 = 机器人在的所有群：按群号从小到大，一次最多改 limit（默认 ALL_MAX）个群，剩下的再发一次接着改；
    saved：存档里的一个 dict，记着每个群原来的名片（摘的时候原样改回去）；
    persist()：每改一个群（和签名）之前调一次，把 saved 存到硬盘上（11-08 复查：以前整趟跑完才存）；
    notify(要改的群, 签名改不改)：真改之前先说一声（插件拿它私聊管理员）。
    → {"done": [(群, 新名片)], "had": [已经带着标记的群], "fail": [(群, 为什么)], "sign": "done" / "had" / "fail…" / "",
       "total": 一共几个群, "left": 这次没看的群有几个}"""
    now = time.time() if now is None else float(now)
    sid = int(self_id)
    gs = saved.setdefault("groups", {})
    rep = {"done": [], "had": [], "fail": [], "sign": "", "total": 0, "left": 0}
    gids = [str(g).strip() for g in gids or () if str(g).strip()]
    if not gids:
        data = await call("get_group_list")
        gids = sorted(
            {
                str(g.get("group_id"))
                for g in data or []
                if isinstance(g, dict) and g.get("group_id")
            },
            key=_gkey,
        )
        limit = ALL_MAX if limit is None else limit
    rep["total"] = len(gids)
    plan, seen = [], 0
    for g in gids:
        if limit is not None and len(plan) >= limit:
            break
        seen += 1
        try:
            info = (
                await call(
                    "get_group_member_info", group_id=int(g), user_id=sid, no_cache=True
                )
                or {}
            )
        except Exception as e:
            rep["fail"].append((g, "读不到现在的名片：" + _err(e)))
            continue
        card, nick = str(info.get("card") or ""), str(info.get("nickname") or "")
        # 名片空着时群里显示的是昵称：昵称已经带着标记就不用再加
        if find_tag(card or nick, rx):
            rep["had"].append(g)
            continue
        plan.append((g, card, with_tag(card or nick, tag)))
    rep["left"] = len(gids) - seen
    sp = None
    if sign:
        try:
            old = sign_of(await call("get_stranger_info", user_id=sid, no_cache=True))
            if find_tag(old, rx):
                rep["sign"] = "had"
            else:
                sp = (old, with_tag(old, tag))
        except Exception as e:
            rep["sign"] = "fail：读不到现在的签名 " + _err(e)
    if notify is not None and (plan or sp):
        try:
            await notify([g for g, _o, _n in plan], sp is not None)
        except Exception:
            pass  # 说不出去也照改：改了哪些在指令的回复里还会再写一遍
    for i, (g, old, new) in enumerate(plan):
        if i:
            await _pause(gap, sleep)
        # 先把原来的名片记下、存到硬盘上，再改：改到一半出了事（重启、断电），原来的名片也找得回来
        gs[g] = {"old": old, "new": new, "t": now, "ok": False}
        if not _persist(persist):
            gs.pop(g, None)
            rep["fail"].append((g, "原来的名片存不到硬盘上，没改"))
            continue
        try:
            await call("set_group_card", group_id=int(g), user_id=sid, card=new)
            gs[g]["ok"] = True
            rep["done"].append((g, new))
        except Exception as e:
            rep["fail"].append((g, _err(e)))
    if sp:
        saved["sign"] = {"old": sp[0], "new": sp[1], "t": now, "ok": False}
        if not _persist(persist):
            saved.pop("sign", None)
            rep["sign"] = "fail：原来的签名存不到硬盘上，没改"
        else:
            try:
                await call("set_self_longnick", longNick=sp[1])
                saved["sign"]["ok"] = True
                rep["sign"] = "done"
            except Exception as e:
                rep["sign"] = "fail：" + _err(e)
    _persist(persist)
    return rep


async def take_tags(call, self_id, saved, gap=1.0, sleep=asyncio.sleep, persist=None):
    """一键摘标记：挂的时候存下的群名片 / 签名原样改回去（改回一个存一次盘）。
    名片后来又被人改过（不是挂上去的那个样子）→ 不动（别把人家后来改的冲掉），记进 changed 告诉管理员原来是什么。
    → {"done": [(群, 改回的名片)], "changed": [(群, 现在的, 原来的)], "fail": [(群, 为什么)], "sign": "done" / "changed" / "fail…" / ""}"""
    sid = int(self_id)
    gs = saved.setdefault("groups", {})
    rep = {"done": [], "changed": [], "fail": [], "sign": ""}
    for i, (g, r) in enumerate(sorted(gs.items())):
        if i:
            await _pause(gap, sleep)
        try:
            info = (
                await call(
                    "get_group_member_info", group_id=int(g), user_id=sid, no_cache=True
                )
                or {}
            )
            cur = str(info.get("card") or "")
        except Exception as e:
            rep["fail"].append((g, "读不到现在的名片：" + _err(e)))
            continue  # 存的留着：下次再摘
        old = str(r.get("old") or "")
        if cur != str(r.get("new") or ""):
            gs.pop(g, None)
            if cur == old:  # 已经被人手动改回原样了
                rep["done"].append((g, old))
            elif r.get("ok"):  # 没 ok 的是当初就没改成：本来就是原样，不用说
                rep["changed"].append((g, cur, old))
            continue
        try:
            await call("set_group_card", group_id=int(g), user_id=sid, card=old)
            gs.pop(g, None)
            rep["done"].append((g, old))
            _persist(persist)
        except Exception as e:
            rep["fail"].append((g, _err(e)))
    s = saved.get("sign")
    if s:
        try:
            cur = sign_of(await call("get_stranger_info", user_id=sid, no_cache=True))
            if cur != str(s.get("new") or ""):
                saved.pop("sign", None)
                if cur == str(s.get("old") or ""):
                    rep["sign"] = "done"
                else:
                    rep["sign"] = "changed" if s.get("ok") else ""
            else:
                await call("set_self_longnick", longNick=str(s.get("old") or ""))
                saved.pop("sign", None)
                rep["sign"] = "done"
        except Exception as e:
            rep["sign"] = "fail：" + _err(e)
    _persist(persist)
    return rep


def _gl(gs, n=20):
    gs = list(gs)
    return "、".join(gs[:n]) + (f" 等 {len(gs)} 个" if len(gs) > n else "")


def tag_report(rep, on=True):
    """挂（on）/ 摘的结果 → (短的, 明细, 有没有要私聊的明细)。
    短的只有个数：在群里发的指令就在群里回这个 —— 群号、名片叫什么别晒给群友（机器人在哪些群是管理员的事，11-08 复查）；
    明细带群号和名片：私聊给发指令的管理员（私聊里发的指令直接回明细）"""
    done, had = rep.get("done") or [], rep.get("had") or []
    changed, fail = rep.get("changed") or [], rep.get("fail") or []
    st, left = str(rep.get("sign") or ""), int(rep.get("left") or 0)
    act = "挂好了" if on else "改回去了"
    rows, parts = [], []
    if done:
        rows.append(
            f"群名片{act}（{len(done)} 个群）："
            + "、".join(f"群 {g}「{c}」" for g, c in done[:20])
            + (f" 等 {len(done)} 个" if len(done) > 20 else "")
        )
        parts.append(f"群名片{act} {len(done)} 个群")
    if had:
        rows.append("已经带着标记、没动：群 " + _gl(had))
        parts.append(f"{len(had)} 个群已经带着标记")
    if changed:
        rows.append(
            "这几个群的名片后来又改过，没动（要摘请自己改）："
            + "、".join(f"群 {g} 现在「{c}」、挂之前「{o}」" for g, c, o in changed[:10])
        )
        parts.append(f"{len(changed)} 个群的名片后来又改过、没动")
    if fail:
        rows.append(
            ("没改成：" if on else "没改成（存的还留着，可以再发一次）：")
            + "、".join(f"群 {g}（{w}）" for g, w in fail[:10])
        )
        parts.append(f"{len(fail)} 个群没改成")
    if st == "done":
        rows.append(f"签名后面加上了「 {SELF_TAG}」" if on else "签名改回去了")
        parts.append("签名也加上了" if on else "签名也改回去了")
    elif st == "had":
        rows.append("签名已经带着标记、没动")
        parts.append("签名已经带着标记")
    elif st == "changed":
        rows.append("签名后来又改过，没动")
        parts.append("签名后来又改过、没动")
    elif st.startswith("fail"):
        rows.append("签名没改成：" + st[5:])
        parts.append("签名没改成")
    if left:
        msg = (
            f"你的机器人在 {rep.get('total', 0)} 个群；没配「挂标记的群」时一次最多改 {ALL_MAX} 个，"
            f"还有 {left} 个群这次没看：再发一次「/认ai 挂标记」接着挂，或者在配置里填上要挂的群"
        )
        rows.append(msg + "。")
        parts.append(f"还有 {left} 个群这次没看（一次最多改 {ALL_MAX} 个，再发一次接着挂）")
    if on and (done or st == "done"):
        rows.append("原来的存下了：/认ai 摘标记 原样改回去。")
    has = bool(done or changed or fail or st == "done" or st.startswith("fail"))
    return ("；".join(parts) + "。" if parts else "没有要改的。"), ("\n".join(rows) or "没有要改的。"), has
