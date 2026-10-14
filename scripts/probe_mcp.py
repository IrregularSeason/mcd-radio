"""探测麦当劳 MCP Server：验证 Token 并拉取工具清单。

用法:
    python3 probe_mcp.py            # 列出所有 Tool
    python3 probe_mcp.py <tool> [json参数]   # 调用指定 Tool
"""

import json
import os
import sys
import urllib.error
import urllib.request

MCP_URL = "https://mcp.mcd.cn"
CONFIG = os.path.expanduser("~/.workbuddy/mcp.json")


def load_token():
    """优先取环境变量 MCD_MCP_TOKEN，便于在 CI 或他人机器上运行；
    否则回退到 WorkBuddy 的自定义连接器配置。"""
    env = os.environ.get("MCD_MCP_TOKEN")
    if env:
        return env if env.lower().startswith("bearer") else "Bearer " + env
    with open(CONFIG) as f:
        cfg = json.load(f)
    return cfg["mcpServers"]["mcd-mcp"]["headers"]["Authorization"]


def rpc(method, params=None, sid=None, notify=False):
    body = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        body["params"] = params
    if not notify:
        body["id"] = 1
    req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", load_token())
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.headers.get("Mcp-Session-Id"), r.read().decode()
    except urllib.error.HTTPError as e:
        return None, "HTTPERROR %s: %s" % (e.code, e.read().decode()[:800])


def parse(txt):
    out = []
    for line in txt.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:].strip()))
            except ValueError:
                pass
    if not out:
        try:
            out = [json.loads(txt)]
        except ValueError:
            pass
    return out


def open_session():
    sid, resp = rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcd-probe", "version": "0.1"},
        },
    )
    # 该 Server 是无状态实现，不返回 Mcp-Session-Id，此时直接用无 session 模式调用
    if sid:
        rpc("notifications/initialized", {}, sid=sid, notify=True)
    return sid


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--schema":
        sid = open_session()
        want = set(sys.argv[2:])
        _, resp = rpc("tools/list", {}, sid=sid)
        for m in parse(resp):
            for t in m.get("result", {}).get("tools", []):
                if t["name"] in want:
                    print("=" * 60)
                    print(t["name"])
                    print(json.dumps(t.get("inputSchema", {}), ensure_ascii=False, indent=2))
        return
    sid = open_session()
    if len(sys.argv) == 1:
        _, resp = rpc("tools/list", {}, sid=sid)
        msgs = parse(resp)
        for m in msgs:
            for t in m.get("result", {}).get("tools", []):
                print("-", t["name"], "|", (t.get("description") or "").replace("\n", " ")[:90])
        return
    tool = sys.argv[1]
    rest = [a for a in sys.argv[2:] if not a.startswith("--")]
    args = json.loads(rest[0]) if rest else {}
    _, resp = rpc("tools/call", {"name": tool, "arguments": args}, sid=sid)
    for m in parse(resp):
        res = m.get("result", {})
        if "--raw" in sys.argv:
            for c in res.get("content", []):
                if c.get("type") == "text":
                    print(c["text"])
            continue
        sc = res.get("structuredContent")
        if sc is not None:
            print(json.dumps(sc, ensure_ascii=False, indent=2))
        else:
            print(json.dumps(m, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
