#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跟天使之心一起装：被我们拦下的那一轮替它记成结束（不装 AstrBot 也能跑）：python tests/test_angel.py [插件目录 默认上一层]"""

import importlib.util
import sys
from pathlib import Path

d = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
spec = importlib.util.spec_from_file_location("angel", d / "angel.py")
angel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(angel)
RES = []


def check(c, m, why=""):
    RES.append((bool(c), m))
    print(("  [过]   " if c else "  [没过] ") + m + ("" if c or not why else f"\n           {str(why)[:600]}"))


class Ledger:
    def __init__(self):
        self.done = []

    def complete_work(self, chat_id, work_id, status="done", result_summary=""):
        self.done.append((chat_id, work_id, status, result_summary))


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class Ctx:
    def __init__(self, star=None, boom=False):
        self.star, self.boom = star, boom

    def get_registered_star(self, name):
        if self.boom:
            raise RuntimeError("x")
        return self.star if name == angel.ANGEL else None


class Ev:
    def __init__(self, wid=""):
        self.extra = {"angelheart_work_id": wid} if wid else {}
        self.unified_msg_origin = "aiocqhttp:GroupMessage:123"

    def get_extra(self, k, default=None):
        return self.extra.get(k, default)

    def set_extra(self, k, v):
        self.extra[k] = v


print("[跟天使之心一起装：拦下的那一轮替它记成结束]")
led = Ledger()
star = Obj(activated=True, star_cls=Obj(angel_context=Obj(work_ledger=led)))
ev = Ev("w1")
ok = angel.release_angel(Ctx(star), ev, "测试")
check(
    ok and led.done == [("aiocqhttp:GroupMessage:123", "w1", "failed", "被别的插件拦下：测试")],
    "天使之心放行过的这一轮（事件上有它的 work_id）→ 它的工作账本里记成结束（不用等 300 秒）",
    led.done,
)
check(not angel.release_angel(Ctx(star), ev, "再来一次") and len(led.done) == 1, "同一轮只记一次", led.done)
check(not angel.release_angel(Ctx(star), Ev(""), "x") and len(led.done) == 1, "事件上没有它的 work_id（它没放行过）：不动")
check(not angel.release_angel(Ctx(None), Ev("w2"), "x"), "没装天使之心：不动、不报错")
check(not angel.release_angel(Ctx(Obj(activated=False, star_cls=None)), Ev("w3"), "x"), "装了没开：不动")
check(not angel.release_angel(Ctx(Obj(activated=True, star_cls=Obj())), Ev("w4"), "x"), "它改了内部结构（找不到工作账本）：不动、不报错")
check(not angel.release_angel(Ctx(star, boom=True), Ev("w5"), "x"), "查插件出错：不动、不报错")


class BadLedger:
    def complete_work(self, *a, **k):
        raise ValueError("x")


check(
    not angel.release_angel(Ctx(Obj(activated=True, star_cls=Obj(angel_context=Obj(work_ledger=BadLedger())))), Ev("w6"), "x"),
    "它的工作账本报错：吞掉，不影响我们",
)

print("\n[main.py 里每一处拦下（stop_event）/ 清空结果不发（clear_result）之前，都先调了 release_angel]")
# 不装 AstrBot 也能查：读 main.py 的语法树，看每个 stop_event() / clear_result() 的前一句是不是 release_angel(...)
# （11-08 复查：「别跟着复读」那处清空结果之前漏了 —— 天使之心那一轮要等 300 秒超时）
import ast


def _call_name(stmt):
    if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
        return ""
    f = stmt.value.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""


def _blocks(node):
    for field in ("body", "orelse", "finalbody", "handlers"):
        v = getattr(node, field, None)
        if isinstance(v, list) and v and isinstance(v[0], ast.stmt):
            yield v
        elif isinstance(v, list):
            for h in v:
                if isinstance(h, ast.ExceptHandler):
                    yield h.body


tree = ast.parse((d / "main.py").read_text(encoding="utf-8"))
stops, bad = [], []
for node in ast.walk(tree):
    for block in _blocks(node):
        for i, st in enumerate(block):
            if _call_name(st) in ("stop_event", "clear_result"):
                stops.append(st.lineno)
                if i == 0 or _call_name(block[i - 1]) != "release_angel":
                    bad.append(st.lineno)
check(stops and not bad, f"{len(stops)} 处拦下 / 清空结果，前一句都是 release_angel(...)", f"main.py 第 {bad} 行前面没有")

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print("\n" + "=" * 50 + f"\n  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
for m in bad:
    print("    -", m)
sys.exit(1 if bad else 0)
