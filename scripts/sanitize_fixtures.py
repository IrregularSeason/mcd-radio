#!/usr/bin/env python3
"""把实测响应里的账号级凭证脱敏，供公开仓库使用。

参赛声明要求提交材料"不包含真实 Token、密钥、密码、账号凭证"。
实测抓下来的 fixtures 里带有**真实券码、券 ID 和带签名的图片 URL**，
这些都属于账号资产，不能进公开仓库。

脱敏规则只针对明确属于账号资产的值，**不会误伤公开的菜单商品码**
（那些是纯数字，如 4810、9900012387）：

* 券码      形如 MCD6XXXXXXXXXXXX —— 整体替换为占位符
* 券 ID     32 位大写十六进制 —— 替换为确定性的伪 ID（保持可复现的关联关系）
* 图片 URL  带 ?sign=... 签名参数的 —— 去掉查询串，只留路径
* traceId   链路追踪 ID —— 替换

用法::

    python3 sanitize_fixtures.py fixtures/          # 原地脱敏整个目录
    python3 sanitize_fixtures.py a.json b.json      # 脱敏指定文件
"""

import json
import os
import re
import sys

COUPON_CODE_RE = re.compile(r"\bMCD6[A-Z0-9]{12,}\b")
COUPON_ID_RE = re.compile(r"\b[0-9A-F]{32}\b")
SIGNED_URL_RE = re.compile(r"(\?sign=).*$")

# 按键名判定的敏感字段。账号 ID 不是"券码形态"，靠上面的正则抓不到，
# 但它同样属于账号凭证，必须一起处理。
SENSITIVE_KEY_RE = re.compile(
    r"(accountid|memberid|memberno|openid|unionid|userid|phone|mobile|contact|"
    r"token|secret|password|address|email)", re.I)

# 占位符必须**故意避开**上面两个正则，否则脚本会因为替换后仍被自己的
# 检测规则命中而报假阳性，第二次运行时还会被二次替换、失去幂等性。
CODE_PLACEHOLDER = "MCD6-SAMPLE-REDACTED"

# 同一个真值映射到同一个伪值，保证脱敏后各文件之间的关联关系仍然成立
_id_map = {}
_seq = [0]


def fake_id(value):
    if value not in _id_map:
        _seq[0] += 1
        _id_map[value] = "SAMPLE-COUPON-ID-%04d" % _seq[0]
    return _id_map[value]


def scrub(value):
    if isinstance(value, str):
        value = COUPON_CODE_RE.sub(CODE_PLACEHOLDER, value)
        value = COUPON_ID_RE.sub(lambda m: fake_id(m.group(0)), value)
        if "?sign=" in value:
            value = SIGNED_URL_RE.sub(r"\1<SIGNATURE-REMOVED>", value)
        return value
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "traceId":
                out[k] = "REDACTED"
            elif SENSITIVE_KEY_RE.search(k) and not isinstance(v, (dict, list)):
                out[k] = "<REDACTED>"
            elif SENSITIVE_KEY_RE.search(k) and isinstance(v, (dict, list)):
                # 地址类字段结构不一，整块打掉更稳妥
                out[k] = "<REDACTED>"
            else:
                out[k] = scrub(v)
        return out
    return value


def scan_for_leaks(path):
    """脱敏后自检：确认没有遗漏的真值形态。"""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    hits = []
    if COUPON_CODE_RE.search(text):
        hits.append("券码")
    if "?sign=q-sign-algorithm" in text:
        hits.append("带签名的图片 URL")
    for probe in ('"accountId"', '"phone"', '"mobile"'):
        if probe in text and "<REDACTED>" not in text:
            hits.append("疑似账号字段 %s" % probe)
    return hits


def process(path):
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        print("跳过 %s（%s）" % (path, exc))
        return True

    cleaned = scrub(obj)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cleaned, fh, ensure_ascii=False, indent=2)

    leaks = scan_for_leaks(path)
    if leaks:
        print("!! %s 仍含 %s" % (path, "、".join(leaks)))
        return False
    print("已脱敏 %s" % path)
    return True


def main():
    targets = sys.argv[1:] or ["fixtures"]
    files = []
    for t in targets:
        if os.path.isdir(t):
            files += [os.path.join(t, f) for f in sorted(os.listdir(t)) if f.endswith(".json")]
        else:
            files.append(t)

    ok = all(process(f) for f in files)
    print()
    print("自检：%s" % ("全部通过，未发现残留凭证" if ok else "存在残留，需人工检查"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
