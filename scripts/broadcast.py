#!/usr/bin/env python3
"""麦门电台 —— 一键播出一期节目。

把「取数 -> 写稿 -> 合成音频」串成一条命令，方便验证与离线演示。

    broadcast.py --out-dir episodes/2026-10-14 --point 中薯条
    broadcast.py --offline --out-dir demo            # 用 fixtures 跑，不需要 Token

设计说明
--------

本脚本只负责**取数**与**编排**；写稿由 station.py 完成，合成由 speak.py 完成。
分工的原因：写稿必须是确定性的、可离线复现的，而取数依赖网络与 Token。
把两者混在一个脚本里，就没法在没有 Token 的机器上验证节目内容是否正确。

**点播环节的名称匹配说明**：`query-meals` 只返回商品编码，不返回名称，
所以想按中文名找商品，就必须逐个调 `query-meal-detail`。这是一条真实的
调用链约束，本脚本对扫描范围设了上限并如实报告扫了多少个。
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import probe_mcp as M          # noqa: E402
import station                  # noqa: E402


def call(sid, tool, args=None):
    """调用一个 MCP 工具，返回 structuredContent。"""
    _, resp = M.rpc("tools/call", {"name": tool, "arguments": args or {}}, sid=sid)
    for m in M.parse(resp):
        res = m.get("result") or {}
        sc = res.get("structuredContent")
        if sc is not None:
            err = station.check_error(sc)
            if err:
                raise RuntimeError("%s 调用失败：%s %s %s"
                                   % (tool, err["code"], err["message"], err["hint"]))
            return sc
    return None


def fetch_online(args):
    """实时取数。"""
    sid = M.open_session()
    print("正在取数 ...")

    bundle = {}
    bundle["now_raw"] = call(sid, "now-time-info")
    now = station.parse_dt(station.unwrap(bundle["now_raw"]).get("formatted"))
    bundle["now"] = now.strftime("%Y-%m-%d %H:%M:%S")
    print("  服务器时间：%s" % bundle["now"])

    bundle["calendar"] = call(sid, "campaign-calendar")
    print("  活动日历：%d 天" % len(station.unwrap(bundle["calendar"]).get("dailyList") or []))

    bundle["coupons"] = call(sid, "query-my-coupons")
    c = station.unwrap(bundle["coupons"])
    print("  券包：%s 张" % c.get("totalCount", len(c.get("coupons") or [])))

    bundle["account"] = call(sid, "query-my-account")
    a = station.unwrap(bundle["account"]) or {}
    print("  积分：可用 %s，已过期 %s" % (a.get("availablePoint"), a.get("expiredPoint")))

    if args.point:
        bundle["request"] = fetch_request(sid, args)
    return bundle


CACHE_PATH = os.path.join(HERE, "..", "cache", "meal-names.json")


def load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=2)


def find_by_name(sid, store_code, codes, keyword, cache, scan_limit):
    """按中文名找商品编码。

    `query-meals` 只返回编码、不返回名称，所以按名字找商品只能逐个调
    `query-meal-detail`。一家门店的完整菜单有 100 多个编码，每次都全扫
    既慢又浪费限流额度——所以把结果按门店缓存下来，只扫没查过的编码。
    """
    store_cache = cache.setdefault(store_code, {})
    pending = [c for c in codes if c not in store_cache]
    if scan_limit:
        pending = pending[:scan_limit]

    if pending:
        print("  本地缓存已有 %d 个名称，本次新查 %d 个 ..."
              % (len(store_cache), len(pending)))
    scanned = 0
    for cd in pending:
        try:
            detail = call(sid, "query-meal-detail",
                          {"orderType": 1, "beType": 1, "storeCode": store_code, "code": cd})
        except RuntimeError:
            continue
        data = station.unwrap(detail) or {}
        name = (data.get("name") or "").strip()
        if name:
            store_cache[cd] = name
        scanned += 1
        if keyword in name:
            save_cache(cache)
            return cd, name, scanned, len(pending)
    save_cache(cache)

    for cd, name in store_cache.items():
        if keyword in name:
            return cd, name, scanned, len(pending)
    return None, None, scanned, len(pending)


def fetch_request(sid, args):
    """点播环节：找门店 -> 找商品 -> 核价。"""
    stores = call(sid, "query-nearby-stores",
                  {"beType": 1, "searchType": 2, "city": args.city, "keyword": args.keyword})
    slist = station.unwrap(stores)
    if isinstance(slist, dict):
        slist = slist.get("stores") or slist.get("data") or []
    if not slist:
        print("  ! 没找到门店，跳过点播环节")
        return None
    store = slist[0]
    code = store.get("storeCode") or store.get("code")
    print("  门店：%s（%s）" % (store.get("storeName") or store.get("name"), code))

    meals = call(sid, "query-meals", {"orderType": 1, "beType": 1, "storeCode": code})
    mdata = station.unwrap(meals) or {}
    codes = []
    for cat in mdata.get("categories") or []:
        for item in cat.get("meals") or []:
            if item.get("code"):
                codes.append(item["code"])
    codes = list(dict.fromkeys(codes))

    target = args.point_code
    matched = None
    if target:
        detail = call(sid, "query-meal-detail",
                      {"orderType": 1, "beType": 1, "storeCode": code, "code": target})
        matched = (station.unwrap(detail) or {}).get("name")
    else:
        print("  菜单有 %d 个商品编码（不含名称），按名字匹配「%s」..." % (len(codes), args.point))
        cache = load_cache()
        target, matched, scanned, _pending = find_by_name(
            sid, code, codes, args.point, cache, args.scan_limit)
        print("  本次实际查询 %d 个编码" % scanned)
        if target:
            detail = call(sid, "query-meal-detail",
                          {"orderType": 1, "beType": 1, "storeCode": code, "code": target})

    if not target:
        print("  ! 没匹配到「%s」，跳过点播环节" % args.point)
        return None

    print("  点播商品：%s（%s）" % (matched, target))
    price = call(sid, "calculate-price", {
        "orderType": 1, "beType": 1, "storeCode": code,
        "items": [{"productCode": target, "quantity": 1}],
    })
    p = station.unwrap(price) or {}
    print("  核价：实付 %s 分" % p.get("price"))

    return {"keyword": args.point, "store": stores, "meal": detail, "price": price}


def fetch_offline(fixtures):
    """用已保存的实测响应跑，不需要 Token。"""
    bundle = {}
    for key, name in [("now_raw", "now-time-info.json"),
                      ("calendar", "campaign-calendar.json"),
                      ("coupons", "my-coupons.json"),
                      ("account", "my-account.json")]:
        path = os.path.join(fixtures, name)
        if not os.path.exists(path):
            print("离线模式缺少 %s" % path, file=sys.stderr)
            return None
        with open(path, encoding="utf-8") as fh:
            bundle[key] = json.load(fh)
    data = station.unwrap(bundle["now_raw"]) or {}
    bundle["now"] = data.get("formatted") or "2026-10-14 14:13:11"

    # 点播环节的三份样本齐了才拼装，缺一份就整段跳过（不半真半假地播）
    p_store = os.path.join(fixtures, "nearby-stores.json")
    p_meal = os.path.join(fixtures, "meal-detail.json")
    p_price = os.path.join(fixtures, "calculate-price.json")
    if all(os.path.exists(p) for p in (p_store, p_meal, p_price)):
        def rd(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        store = rd(p_store)
        item = station.unwrap(rd(p_meal)) or {}
        bundle["request"] = {
            "keyword": item.get("name"),
            "store": store, "meal": rd(p_meal), "price": rd(p_price),
        }

    print("离线模式：使用 %s 的实测响应" % bundle["now"])
    return bundle


def main():
    ap = argparse.ArgumentParser(description="麦门电台一键播出")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--offline", action="store_true", help="用 fixtures 跑，不需要 Token")
    ap.add_argument("--fixtures", default=os.path.join(HERE, "..", "fixtures"))
    ap.add_argument("--point", default=None, help="点播的餐品名，如「中薯条」")
    ap.add_argument("--point-code", default=None, help="直接给商品编码，跳过名称匹配")
    ap.add_argument("--city", default="深圳")
    ap.add_argument("--keyword", default="万象城")
    ap.add_argument("--scan-limit", type=int, default=0,
                    help="名称匹配时最多新查多少个编码，0 表示不限（有缓存，通常只有首期较慢）")
    ap.add_argument("--listener", default=None, help="听众称呼")
    ap.add_argument("--no-audio", action="store_true")
    args = ap.parse_args()

    bundle = fetch_offline(args.fixtures) if args.offline else fetch_online(args)
    if not bundle:
        return 1

    if args.listener:
        bundle["listener"] = {"name": args.listener}

    now = station.parse_dt(bundle["now"])
    out_dir = args.out_dir or os.path.join(HERE, "..", "episodes", now.date().isoformat())

    info, segments = station.build(bundle)
    station.write_script(info, segments, out_dir)
    print()
    print("%s %s · 第 %d 期《%s》" % (info["station"], info["frequency"],
                                     info["episode"], info["show"]))
    print("全文 %d 字，预计 %d 分 %d 秒" % (
        info["chars"], station.estimate_seconds(info["chars"]) // 60,
        station.estimate_seconds(info["chars"]) % 60))
    print("产物：%s/{script.md,lines.txt,meta.json,card.html}" % os.path.abspath(out_dir))

    with open(os.path.join(out_dir, "bundle.json"), "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, ensure_ascii=False, indent=2)

    if not args.no_audio:
        print()
        cmd = [sys.executable, os.path.join(HERE, "speak.py"),
               os.path.join(out_dir, "lines.txt"),
               "--out", os.path.join(out_dir, "episode.m4a"),
               "--voice", info["voice"]]
        rc = subprocess.call(cmd)
        if rc != 0:
            print("音频合成未成功（退出码 %d），节目单仍会生成。" % rc)

    # 卡片放在音频之后生成，这样同目录已有音频时能把播放器一起嵌进去
    for cand in ("episode.m4a", "episode.mp3", "episode.aiff"):
        if os.path.exists(os.path.join(out_dir, cand)):
            info["has_audio"] = True
            info["audio"] = cand
            break
    station.write_card(info, segments, os.path.join(out_dir, "card.html"))
    print("节目单卡片：%s%s" % (os.path.join(out_dir, "card.html"),
                              "（已内嵌播放器）" if info.get("has_audio") else ""))
    if args.no_audio:
        print("已跳过音频合成（--no-audio）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
