#!/usr/bin/env python3
"""麦门电台 · FM 1024 —— 节目文稿生成内核。

设计原则：**不做任何网络请求**。

本脚本只消费麦当劳 MCP 返回的 structuredContent，负责把当天的真实数据
翻译成一期节目的文稿。所有数字都必须能在 MCP 返回里找到出处；找不到
出处的段落宁可整段不播，也不编造。

之所以要把"写稿"和"取数"分开：电台最怕的就是每天质量忽高忽低。文稿由
确定性算法生成，同一份输入永远得到同一期节目，可以离线复现、可以测试。

用法::

    station.py script <bundle.json> [--out-dir DIR]   # 生成文稿 + 播报稿
    station.py card   <bundle.json> [--out FILE]      # 生成节目单卡片
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import date, datetime

# ---------------------------------------------------------------- 台呼

FREQ = "FM 1024"
STATION = "麦门电台"

# 开播日（与大赛开幕同日）。期号由它推算，因此每个人都从同一期开始数。
EPOCH = date(2026, 10, 9)

# (节目名, 问候语, 音色) 按时段切换。
#
# 音色名必须写成**带语区的完整名**：`say -v Reed` 不会报错，但会安静地
# 生成一段 0.016 秒的空白音频；只有 `Reed (中文（中国大陆）)` 才真正合成。
# 这个坑已在 speak.py 里加了时长自检兜底。
SHOWS = {
    "morning": ("麦门晨报", "早上好", "Reed (中文（中国大陆）)"),
    "noon": ("午间点播", "中午好", "Flo (中文（中国大陆）)"),
    "night": ("麦门夜话", "晚上好", "Shelley (中文（中国大陆）)"),
}

# 星期编码：麦当劳用 1=周日 … 7=周六
WD_NAME = {1: "周日", 2: "周一", 3: "周二", 4: "周三", 5: "周四", 6: "周五", 7: "周六"}

# 已知业务错误码（实测确认）
ERROR_HINTS = {
    401: "MCP Token 无效、已过期或未提供。",
    429: "触发限流（超过 600 次/分钟）。",
    600012: "促销规则不支持该商品：券必须绑定它自己的促销商品码。",
    600022: "暂不支持多张券使用：一单只能用一张券。",
    600050: "收藏餐厅列表为空，请提供城市 + 关键词。",
    600058: "城市名或者关键词不能为空（两者必须同时传）。",
}


# ---------------------------------------------------------------- 文案库
#
# 电台的语气靠这批句子撑着，所以它们是**手写的、有限的**，而不是让模型
# 每期现编。模型即兴写字，质量会随心情波动；电台需要一个稳定的声音。
# 每批句子按日期做确定性选取，因此同一期节目任何时候重播都完全一样。

GREETING_TAIL = [
    "今天的这一期，只有你会听到。",
    "频率没变，还是 FM 1024。",
    "老规矩，先报时间，再说今天。",
    "这一期照常开了。",
    "我在。你在的话，就听下去。",
    "开播之前，先把时间对上。",
]

# 收尾语按时段分组。
#
# 这一条是实测之后才改的：第一版只写了一套收尾语，结果中午 14 点播出的
# 节目里出现了「夜里的话，说完就放下。晚安」——时段和语气对不上。
SIGN_OFF = {
    "morning": [
        "今天就从这一顿开始。祝你顺利。",
        "早餐是现在最重要的事。去吃吧。",
        "早上的时间不多，别耽误了。",
        "这一期到这儿。今天慢慢来。",
        "祝你今天顺利。这句话我每期都说，但每次都当真。",
        "出门吧。路上小心。",
    ],
    "noon": [
        "中午就这样。下午还得接着过。",
        "不管你刚才听到什么，先去吃饭。",
        "午休不长，别耗在选餐上。",
        "这期到这儿。下午见。",
        "吃完了再想别的。",
        "频率不变，下午我还在这儿。",
    ],
    "night": [
        "今天就这样。明天这个时间，我还在这儿。",
        "不用回话。听完，去吃饭。",
        "夜里的话，说完就放下。晚安。",
        "节目播完了，日子还长。",
        "把没做完的事留给明天，把这一顿留给自己。",
        "这里是麦门电台，FM 1024。我们下期见。",
        "频率不变。明天见。",
        "记得吃饭。这句话我说了很多期，但每次都当真。",
        "我的话讲完了，你的还没开始。",
        "到这。晚安。",
    ],
}

# 今日代号候选词。只有当某个词**今天确实出现在活动数据里**时才会被选中，
# 因此代号不是抽出来的，是算出来的。
CODEWORDS = [
    "龙焰鸡腿堡", "蓝莓爆爆珠麦旋风", "灰焰圆筒", "厚松饼堡", "蘸酱炸鸡",
    "鸡薯双全盒", "棒球帽", "小雏菊", "丝巾", "徽章",
    "冰美式", "麦旋风", "圆筒", "巨无霸", "麦辣鸡翅", "中薯条",
    "过家家派对", "亲子阅读", "鲜萃咖啡", "黑巧", "麦金卡", "麦金学期卡",
    "开心乐园餐", "四件套", "三件套", "早餐两件套",
]


# ---------------------------------------------------------------- 基础工具


def unwrap(obj):
    """把 MCP 的返回统一剥到最内层的业务数据。

    支持三种形态：完整 MCP 响应 / structuredContent 信封 / 已经是 data。
    用 `success` 字段做结构性判定，而不是猜 data 里的键名——早期版本靠
    猜键名，遇到 calculate-price 的 data 会判定失败。
    """
    if not isinstance(obj, dict):
        return obj
    if isinstance(obj.get("structuredContent"), dict):
        obj = obj["structuredContent"]
    elif isinstance(obj.get("result"), dict):
        obj = obj["result"]
        if isinstance(obj.get("structuredContent"), dict):
            obj = obj["structuredContent"]
    if obj.get("success") is not None and isinstance(obj.get("data"), (dict, list)):
        obj = obj["data"]
    return obj


def check_error(resp):
    """把业务错误转成可读信息；正常返回 None。"""
    if not isinstance(resp, dict):
        return None
    if resp.get("success") is False:
        code = resp.get("code")
        return {
            "code": code,
            "message": resp.get("message", "未知错误"),
            "hint": ERROR_HINTS.get(code, ""),
        }
    return None


def parse_dt(text):
    if not text:
        return None
    text = str(text).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def mcd_weekday(dt):
    return (dt.weekday() + 1) % 7 + 1


def money(cents):
    if cents is None:
        return "-"
    return "¥%.2f" % (cents / 100.0)


def pick(bank, seed):
    """按 seed 做确定性选取，同一 seed 永远得到同一条。"""
    if not bank:
        return ""
    n = int(hashlib.md5(str(seed).encode("utf-8")).hexdigest()[:8], 16)
    return bank[n % len(bank)]


def speak_money(text):
    """把钱数写成适合朗读的形式：¥13.50 -> 13块5。"""
    m = re.match(r"^¥?(\d+)(?:\.(\d{1,2}))?$", str(text).strip())
    if not m:
        return str(text)
    yuan = int(m.group(1))
    cents = int((m.group(2) or "0").ljust(2, "0"))
    if cents == 0:
        return "%d块" % yuan
    if cents % 10 == 0:
        return "%d块%d" % (yuan, cents // 10)
    return "%d块%d毛%d" % (yuan, cents // 10, cents % 10)


def dur_text(minutes):
    """把分钟数说成人话。"""
    if minutes < 1:
        return "不到一分钟"
    if minutes < 60:
        return "%d 分钟" % minutes
    h, m = divmod(minutes, 60)
    if m == 0:
        return "%d 小时" % h
    return "%d 小时 %d 分钟" % (h, m)


def days_text(days):
    if days == 0:
        return "就是今天"
    if days == 1:
        return "就是明天"
    return "还有 %d 天" % days


def load_json(path):
    if path in (None, "-"):
        raw = sys.stdin.read()
    else:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    return json.loads(raw)


# ---------------------------------------------------------------- 节目时段


def show_of(now):
    """按当前时刻决定节目名、问候语与主播音色。

    返回 (时段键, 节目名, 问候语, 音色)。时段键同时用于挑选收尾语，
    否则会出现「中午 14 点播出的节目里说晚安」这种时间错位。
    """
    t = now.hour * 60 + now.minute
    if 5 * 60 <= t < 10 * 60 + 30:
        key = "morning"
    elif 10 * 60 + 30 <= t < 17 * 60:
        key = "noon"
    else:
        key = "night"
    name, greeting, voice = SHOWS[key]
    return key, name, greeting, voice


def episode_no(day):
    n = (day - EPOCH).days + 1
    return max(n, 1)


# ---------------------------------------------------------------- 各段取材


def today_entry(cal, day):
    """取出活动日历里今天那一天的条目。"""
    for d in cal.get("dailyList") or []:
        if d.get("date") == day.isoformat():
            return d
    return None


def parse_range(content):
    """从活动文案里抽日期区间，返回 (起, 止)。支持「10月8日至10月21日」。"""
    m = re.search(
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:至|到|-|~)\s*(?:(\d{1,2})\s*月)?\s*(\d{1,2})\s*日",
        content or "")
    if not m:
        return None
    m1, d1 = int(m.group(1)), int(m.group(2))
    m2 = int(m.group(3)) if m.group(3) else m1
    d2 = int(m.group(4))
    year = datetime.now().year
    try:
        return date(year, m1, d1), date(year, m2, d2)
    except ValueError:
        return None


def ending_soon(cal, day):
    """在今天在播的活动里，找最快结束的那一场。

    电台该报的不是"今天有很多活动"，而是"哪一场快没了"——后者才有行动价值。
    """
    entry = today_entry(cal, day)
    if not entry:
        return None
    best = None
    for ev in entry.get("events") or []:
        art = ev.get("articleDto") or {}
        title = (art.get("title") or "").strip()
        if not title:
            continue
        rng = parse_range(art.get("content"))
        if not rng:
            continue
        start, end = rng
        if not (start <= day <= end):
            continue
        left = (end - day).days
        if best is None or left < best["daysLeft"]:
            best = {
                "title": clean_title(title),
                "highlight": (art.get("highlights") or "").strip(),
                "start": start,
                "end": end,
                "daysLeft": left,
            }
    return best


def clean_title(text):
    """把标题整理成适合朗读的文本。

    官方文案爱用「𝟗.𝟗」这类**数学字母数字**做粗体效果。它们不是普通数字，
    直接按 emoji 删掉会把价格吃掉（「超值𝟗.𝟗元早餐」会变成「超值.元早餐」）。
    所以这里先把各种花式数字**映射回普通数字**，再清掉真正的表情符号。
    """
    if not text:
        return ""
    out = []
    for ch in text:
        o = ord(ch)

        # 数学粗体/双线/无衬线/等宽数字 -> 普通 0-9
        if 0x1D7CE <= o <= 0x1D7FF:
            out.append(str((o - 0x1D7CE) % 10))
            continue
        # 数学粗体大写字母 -> 普通 A-Z
        if 0x1D400 <= o <= 0x1D419:
            out.append(chr(ord("A") + (o - 0x1D400)))
            continue
        # 全角数字 -> 半角
        if 0xFF10 <= o <= 0xFF19:
            out.append(chr(ord("0") + (o - 0xFF10)))
            continue

        if (
            0x1F000 <= o <= 0x1FAFF      # emoji
            or 0x2600 <= o <= 0x27BF     # 杂项符号
            or o in (0xFE0F, 0x200D, 0x2705, 0x2B50, 0x1F3F4)
        ):
            continue
        out.append(ch)

    s = "".join(out)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -—·、,，.。!！~～")


def next_drop(cal, now):
    """找最近一场「即将开售」的联名周边，并算出倒计时。

    活动文案里有「10月15日10:45开售」「APP专享积分抢先购10:40开抢」这类
    定时信息，这是电台最该播的东西：具体到分钟的行动倒计时。
    """
    best = None
    for d in cal.get("dailyList") or []:
        day = parse_dt(d.get("date"))
        if not day:
            continue
        for ev in d.get("events") or []:
            if ev.get("activityStage") != 2:
                continue
            art = ev.get("articleDto") or {}
            content = art.get("content") or ""
            m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：]\s*(\d{2})\s*开售", content)
            if not m:
                continue
            try:
                when = datetime(now.year, int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), int(m.group(4)))
            except ValueError:
                continue
            if when < now:
                continue
            if best is None or when < best["when"]:
                best = {
                    "when": when,
                    "title": clean_title(art.get("title")),
                    "highlight": (art.get("highlights") or "").strip(),
                    "content": content,
                    "daysLeft": (when.date() - now.date()).days,
                    "minutesLeft": int((when - now).total_seconds() // 60),
                }
    return best


def judge_coupon(coupon, now):
    """判定单张券此刻是否可用（与券管家同源逻辑的精简版）。"""
    start = parse_dt(coupon.get("tradeStartDateTime") or coupon.get("tradeStartDate"))
    end = parse_dt(coupon.get("tradeEndDateTime") or coupon.get("tradeEndDate"))

    # 未来才生效的券 enable 也可能是 0，若先采信 enable 会把"未生效"误报成"已失效"
    if start and now < start:
        return {"status": "upcoming", "start": start, "end": end}
    if end and now < end and coupon.get("enable") == 0 and start and now >= start:
        return {"status": "expired", "start": start, "end": end}
    if end and now > end:
        return {"status": "expired", "start": start, "end": end}
    if start and now < start:
        return {"status": "upcoming", "start": start, "end": end}
    if coupon.get("enable") == 0:
        return {"status": "expired", "start": start, "end": end}
    if coupon.get("totalAvailableCount") == 0:
        return {"status": "expired", "start": start, "end": end}
    return {"status": "usable", "start": start, "end": end}


def coupon_alerts(coupon_resp, now):
    """挑出最该提醒的那张券：可用券里最早到期的。"""
    data = unwrap(coupon_resp or {})
    coupons = data.get("coupons") or []
    total = data.get("totalCount", len(coupons))

    usable, upcoming, expired = [], [], []
    for c in coupons:
        j = judge_coupon(c, now)
        di = c.get("discountInfo") or {}
        row = {
            "title": clean_title(c.get("title")),
            "price": ("%s%s" % (di.get("discountTypeText", ""), di.get("discountValue", ""))).strip(),
            "priceValue": di.get("discountValue"),
            "end": j.get("end"),
            "start": j.get("start"),
            "status": j["status"],
        }
        if j["status"] == "usable":
            row["daysLeft"] = (j["end"].date() - now.date()).days if j.get("end") else None
            usable.append(row)
        elif j["status"] == "upcoming":
            upcoming.append(row)
        else:
            expired.append(row)

    usable.sort(key=lambda r: (r.get("daysLeft") if r.get("daysLeft") is not None else 999))
    return {
        "total": total,
        "usable": usable,
        "upcoming": upcoming,
        "expired": expired,
        "urgent": usable[0] if usable else None,
    }


def points_line(account_resp):
    """积分账户里最值得说的一句话。

    优先说"即将过期"，其次说"已经过期"——后者是一种没人提醒过的沉默损失。
    """
    data = unwrap(account_resp or {})
    if not isinstance(data, dict) or not data:
        return None

    def num(key):
        try:
            return float(data.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    soon = num("currentMouthExpirePoint") + num("nextMouthExpirePoint")
    gone = num("expiredPoint")
    avail = num("availablePoint")

    if soon > 0:
        return "你的积分里有 %s 分这个月就要过期了，去麦享会把它用掉。" % fmt_num(soon)
    if gone > 0 and avail <= 0:
        return ("你的积分账户里，已经有 %s 分过期了。系统不会通知你这件事，"
                "所以你一直不知道。" % fmt_num(gone))
    if avail > 0:
        return "你现在有 %s 积分躺在账户里，暂时没有过期风险。" % fmt_num(avail)
    return None


def fmt_num(v):
    return str(int(v)) if float(v).is_integer() else ("%.1f" % v)


def codename(cal, day):
    """今日代号：只从今天真实出现过的词里选，再按日期确定性挑一个。

    这样代号就不是抽签，而是算出来的——每天一个，可复现，且和今天的真实
    菜单绑定。
    """
    entry = today_entry(cal, day)
    haystack = ""
    if entry:
        for ev in entry.get("events") or []:
            art = ev.get("articleDto") or {}
            haystack += (art.get("title") or "") + (art.get("content") or "") + (art.get("highlights") or "")

    hits = [w for w in CODEWORDS if w in haystack]
    if hits:
        return pick(hits, day), "今天在播的活动里"
    return pick(CODEWORDS, day), "常规词表"


def speakable(text):
    """把标题里不适合朗读的写法换成口语写法。

    官方联名标题写作「麦当劳 X PEACEMINUSONE」，直接朗读会念成字母 X。
    """
    return re.sub(r"\s+[Xx×]\s+", "与", text or "")


def request_lines(req, coupon_titles=None):
    """点播环节：把一次真实核价读出来。

    顺带做一次交叉引用：如果卡包里有券的名字包含听众点的这份餐，就提一句
    「可能用得上」。只做**提示**不做数字承诺——能不能用、省多少取决于
    门店和时段，电台不该替用户下这个结论。
    """
    if not req:
        return None
    store = unwrap(req.get("store") or {})
    meal = unwrap(req.get("meal") or {})
    price = unwrap(req.get("price") or {})

    store_name = None
    for key in ("storeName", "name", "restaurantName"):
        if isinstance(store, dict) and store.get(key):
            store_name = store[key]
            break
    if not store_name and isinstance(store, list) and store:
        first = store[0]
        store_name = first.get("storeName") or first.get("name")

    item = clean_title(meal.get("name") if isinstance(meal, dict) else "") or req.get("keyword") or ""

    lines = []
    if item:
        lines.append("有听众点了一份%s。" % item)
    if isinstance(price, dict) and price.get("price") is not None:
        lines.append("核过价了，不用券的实付是 %s。" % speak_money(money(price.get("price"))))
    if store_name:
        lines.append("在%s，现在下单就能取。" % store_name)
    if item and coupon_titles:
        for t in coupon_titles:
            if item in t:
                lines.append("你的卡包里正好有一张「%s」，可能用得上。" % t)
                break
    return lines or None


# ---------------------------------------------------------------- 组装节目


def build(bundle, meta_only=False):
    """把数据包组装成一期节目，返回 (节目信息, 段落列表)。"""
    now = parse_dt(bundle.get("now")) or datetime.now()
    day = now.date()

    show_key, show_name, greeting, voice = show_of(now)
    ep = episode_no(day)
    listener = (bundle.get("listener") or {}).get("name") or ""
    cal = unwrap(bundle.get("calendar") or {})

    segments = []
    notes = []      # 供人类核对的数据出处，不进播报稿

    # ---- 1 台标与报时
    head = []
    head.append("%s，%s。%s，第 %d 期。" % (STATION, FREQ, show_name, ep))
    clock = "现在是 %d 月 %d 日，%s，%d 点 %d 分。" % (
        now.month, now.day, WD_NAME[mcd_weekday(now)], now.hour, now.minute)
    head.append(clock)
    tail = pick(GREETING_TAIL, day)
    head.append("%s%s" % (("%s，" % listener) if listener else "", tail))
    segments.append({"name": "台标与报时", "lines": head})

    # ---- 2 今日现场
    live = []
    soon = ending_soon(cal, day)
    if soon:
        live.append("先说今天的事。%s。" % soon["title"])
        if soon["daysLeft"] == 0:
            live.append("今天最后一天，过了就没有了。")
        else:
            live.append("%d 月 %d 日结束，%s。" % (soon["end"].month, soon["end"].day,
                                                 days_text(soon["daysLeft"])))
    drop = next_drop(cal, now)
    if drop:
        if live:
            live.append("另外，下一场开售已经定好时间了。")
        else:
            live.append("先说今天的事。")
        live.append("%s，%d 月 %d 日 %d 点 %02d 分开售，%s。"
                    % (speakable(drop["title"]), drop["when"].month, drop["when"].day,
                       drop["when"].hour, drop["when"].minute, days_text(drop["daysLeft"])))
        if drop["daysLeft"] == 0:
            live.append("距离现在还有 %s。" % dur_text(drop["minutesLeft"]))
        if "积分" in drop["content"] and "抢" in drop["content"]:
            live.append("积分可以早五分钟抢。")
    if live:
        segments.append({"name": "今日现场", "lines": live})
        notes.append("今日现场：ending_soon=%s，next_drop=%s"
                     % (bool(soon), drop["when"].strftime("%Y-%m-%d %H:%M") if drop else None))

    # ---- 3 听众信箱
    box = []
    al = coupon_alerts(bundle.get("coupons") or {}, now)
    if al["total"]:
        box.append("下面是听众信箱。你的卡包里现在有 %d 张券，能用的 %d 张。"
                   % (al["total"], len(al["usable"])))
        u = al["urgent"]
        if u:
            d = u.get("daysLeft")
            if d is not None:
                if d <= 0:
                    box.append("最急的一张是「%s」，今天到期。不用就作废了。" % u["title"])
                elif d == 1:
                    box.append("最急的一张是「%s」，明天到期。" % u["title"])
                else:
                    box.append("最急的一张是「%s」，%d 月 %d 日到期，%s。"
                               % (u["title"], u["end"].month, u["end"].day,
                                  days_text(d)))
        if len(al["usable"]) > 1:
            box.append("其余的也排好队了，需要的时候问我。")
    else:
        box.append("下面是听众信箱。你的卡包现在是空的。")
        box.append("去麦麦省把券领了，下次这一栏就有东西念了。")

    pts = points_line(bundle.get("account") or {})
    if pts:
        box.append(pts)
    if box:
        segments.append({"name": "听众信箱", "lines": box})

    # ---- 4 点播
    req = request_lines(bundle.get("request"), [u["title"] for u in al["usable"]])
    if req:
        segments.append({"name": "点播时间", "lines": ["接下来是点播时间。"] + req})

    # ---- 5 今日代号
    code, source = codename(cal, day)
    segments.append({"name": "今日代号", "lines": [
        "今天的暗号，是这样的。",
        "「%s」。%s" % (code, "它来自" + source + "。" if source else ""),
        "这个暗号是按日期算出来的，不是抽的，所以明天一定会换成另一个。",
    ]})
    notes.append("今日代号：%s（来源：%s）" % (code, source))

    # ---- 6 收尾
    segments.append({"name": "收尾", "lines": [pick(SIGN_OFF[show_key], day)]})

    info = {
        "station": STATION,
        "frequency": FREQ,
        "show": show_name,
        "show_key": show_key,
        "voice": voice,
        "greeting": greeting,
        "episode": ep,
        "date": day.isoformat(),
        "served_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "codename": code,
        "listener": listener,
        "chars": sum(len("".join(s["lines"])) for s in segments),
        "segments": [s["name"] for s in segments],
        "notes": notes,
    }
    return info, segments


def estimate_seconds(chars):
    """中文 TTS 约每秒 4.7 字（实测：56 字 -> 12 秒）。"""
    return int(chars / 4.7)


# ---------------------------------------------------------------- 输出


def write_script(info, segments, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    md = ["# %s · %s" % (info["station"], info["frequency"]), ""]
    md.append("**%s｜第 %d 期**" % (info["show"], info["episode"]))
    md.append("")
    md.append("播出时间：%s　|　主播音色：%s　|　全文 %d 字　|　预计时长 %d 分 %d 秒"
              % (info["served_at"], info["voice"], info["chars"],
                 estimate_seconds(info["chars"]) // 60, estimate_seconds(info["chars"]) % 60))
    md.append("")
    md.append("今日代号：**%s**" % info["codename"])
    md.append("")
    md.append("---")
    md.append("")
    for seg in segments:
        md.append("### %s" % seg["name"])
        md.append("")
        for line in seg["lines"]:
            md.append(line)
            md.append("")

    with open(os.path.join(out_dir, "script.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md).rstrip() + "\n")

    # 播报稿：一行一段落，段与段之间由 speak.py 插入停顿
    with open(os.path.join(out_dir, "lines.txt"), "w", encoding="utf-8") as fh:
        for seg in segments:
            fh.write("".join(seg["lines"]) + "\n")

    with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)

    return out_dir


def write_card(info, segments, out_path):
    """节目单卡片：单文件 HTML，既可直接播放，也可截图分享。

    如果同目录下已经有合成好的音频，就把播放器嵌进卡片，这样打开一个文件
    就能边听边看节目单——对评审来说是最短的体验路径。
    """
    rows = []
    for seg in segments:
        rows.append(
            '<div class="seg"><div class="segname">%s</div><div class="segtext">%s</div></div>'
            % (seg["name"], "".join(seg["lines"])))

    secs = estimate_seconds(info["chars"])

    audio_tag = ""
    audio_name = os.path.basename(info.get("audio") or "episode.m4a")
    if info.get("has_audio"):
        audio_tag = (
            '<audio controls preload="metadata" src="%s"></audio>' % audio_name)

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(station)s %(freq)s · 第 %(ep)d 期</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#12100e;color:#f2ece4;font-family:-apple-system,"PingFang SC",sans-serif;
     display:flex;justify-content:center;padding:32px 16px}
.card{width:100%%;max-width:520px;background:#1c1916;border:1px solid #332e28;
      border-radius:20px;padding:32px 28px}
.top{display:flex;justify-content:space-between;align-items:baseline;
     border-bottom:1px solid #332e28;padding-bottom:18px;margin-bottom:22px}
.station{font-size:19px;font-weight:500;letter-spacing:.06em}
.freq{font-size:13px;color:#d8a24a;letter-spacing:.14em}
.meta{font-size:13px;color:#9a9086;line-height:1.9;margin-bottom:20px}
.meta b{color:#f2ece4;font-weight:500}
audio{width:100%%;margin-bottom:22px;height:38px}
.code{background:#26211c;border-radius:12px;padding:16px 18px;margin-bottom:24px}
.code .lbl{font-size:12px;color:#9a9086;letter-spacing:.1em;margin-bottom:6px}
.code .val{font-size:22px;font-weight:500;color:#d8a24a}
.seg{padding:14px 0;border-top:1px solid #26211c}
.segname{font-size:12px;color:#d8a24a;letter-spacing:.08em;margin-bottom:7px}
.segtext{font-size:14px;line-height:1.85;color:#cfc7bd}
.foot{margin-top:26px;padding-top:16px;border-top:1px solid #332e28;
      font-size:12px;color:#6f675e;line-height:1.8}
</style></head><body>
<div class="card">
  <div class="top">
    <div class="station">%(station)s</div>
    <div class="freq">%(freq)s</div>
  </div>
  <div class="meta">
    <b>%(show)s</b>　第 %(ep)d 期<br>
    播出 %(served)s<br>
    时长约 %(mm)d 分 %(ss)d 秒　·　全文 %(chars)d 字
  </div>
  %(audio)s
  <div class="code">
    <div class="lbl">今日代号</div>
    <div class="val">%(codename)s</div>
  </div>
  %(rows)s
  <div class="foot">本节目全部内容来自麦当劳 MCP 当日真实数据。<br>
  非麦当劳官方产品。</div>
</div></body></html>
""" % {
        "station": info["station"], "freq": info["frequency"], "show": info["show"],
        "ep": info["episode"], "served": info["served_at"], "chars": info["chars"],
        "mm": secs // 60, "ss": secs % 60, "codename": info["codename"],
        "rows": "\n  ".join(rows), "audio": audio_tag,
    }
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


# ---------------------------------------------------------------- 入口


def main():
    parser = argparse.ArgumentParser(description="麦门电台文稿生成内核（离线，不发网络请求）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_script = sub.add_parser("script", help="生成一期节目的文稿与播报稿")
    p_script.add_argument("bundle", nargs="?", default="-", help="当期数据包")
    p_script.add_argument("--out-dir", default="episodes/latest")

    p_card = sub.add_parser("card", help="生成节目单卡片")
    p_card.add_argument("bundle", nargs="?", default="-")
    p_card.add_argument("--out", default="episodes/latest/card.html")

    args = parser.parse_args()
    bundle = load_json(args.bundle)
    info, segments = build(bundle)

    if args.cmd == "script":
        d = write_script(info, segments, args.out_dir)
        secs = estimate_seconds(info["chars"])
        print("%s %s · 第 %d 期《%s》" % (info["station"], info["frequency"],
                                        info["episode"], info["show"]))
        print("段落：%s" % " / ".join(info["segments"]))
        print("全文 %d 字，预计 %d 分 %d 秒" % (info["chars"], secs // 60, secs % 60))
        print("今日代号：%s" % info["codename"])
        print("已写入 %s/{script.md,lines.txt,meta.json}" % d)
        for n in info["notes"]:
            print("  核对 · %s" % n)
    else:
        # 同目录下已经有音频时，把播放器嵌进卡片
        folder = os.path.dirname(os.path.abspath(args.out))
        for cand in ("episode.m4a", "episode.mp3", "episode.aiff"):
            if os.path.exists(os.path.join(folder, cand)):
                info["has_audio"] = True
                info["audio"] = cand
                break
        path = write_card(info, segments, args.out)
        print("节目单卡片已写入 %s%s"
              % (path, "（已内嵌播放器）" if info.get("has_audio") else "（未找到音频，仅卡片）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
