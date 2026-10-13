"""OP.GG MCP 서버(https://mcp-api.op.gg/mcp)를 HTTP로 직접 호출하는 클라이언트.

MCP 응답 텍스트는 아래 같은 독자 포맷이라 parse_opgg_text()로 dict로 바꾼다.

    class Data: summary,damage_type
    class Summary: average_stats
    class AverageStats: play,win_rate

    LolGetChampionAnalysis(Data(Summary(AverageStats(64306,0.5)),"AP"))
"""

import json
import re
import time
import urllib.request

MCP_URL = "https://mcp-api.op.gg/mcp"
PROTOCOL_VERSION = "2025-06-18"


class OpggError(RuntimeError):
    pass


class OpggClient:
    def __init__(self, url=MCP_URL, timeout=60, delay=0.15, retries=3):
        self.url = url
        self.timeout = timeout
        self.delay = delay
        self.retries = retries
        self.session_id = None
        self._next_id = 1

    def _post(self, payload):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            body = resp.read().decode()
            ctype = resp.headers.get("content-type", "")
        if not body.strip():
            return None
        if "text/event-stream" in ctype:
            # SSE 응답이면 마지막 data: 줄이 JSON-RPC 결과다.
            data_lines = [l[5:].strip() for l in body.splitlines() if l.startswith("data:")]
            body = data_lines[-1] if data_lines else "{}"
        return json.loads(body)

    def _rpc(self, method, params=None, notify=False):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = self._next_id
            self._next_id += 1
        return self._post(payload)

    def connect(self):
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "lol-picker", "version": "1.0"},
            },
        )
        self._rpc("notifications/initialized", notify=True)

    def call_raw(self, tool, arguments):
        """도구를 호출하고 응답 텍스트를 그대로 돌려준다."""
        if self.session_id is None:
            self.connect()
        last_err = None
        for attempt in range(self.retries):
            try:
                res = self._rpc("tools/call", {"name": tool, "arguments": arguments})
                if res is None:
                    raise OpggError("빈 응답")
                if "error" in res:
                    raise OpggError(f"{tool}: {res['error']}")
                result = res["result"]
                text = "".join(c.get("text", "") for c in result.get("content", []))
                if result.get("isError"):
                    raise OpggError(f"{tool}: {text}")
                time.sleep(self.delay)
                return text
            except OpggError:
                raise
            except Exception as e:  # 네트워크 오류는 재시도
                last_err = e
                time.sleep(2 ** attempt)
                self.session_id = None
                self.connect()
        raise OpggError(f"{tool} 호출 실패: {last_err}")

    def call(self, tool, arguments):
        """도구를 호출하고 파싱된 dict를 돌려준다."""
        return parse_opgg_text(self.call_raw(tool, arguments))


# ---------------------------------------------------------------------------
# 응답 파서

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<str>"(?:[^"\\]|\\.)*")
      | (?P<num>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<punct>[(),\[\]])
    )""",
    re.VERBOSE,
)


def _tokenize(s):
    pos, out = 0, []
    s = s.rstrip()
    while pos < len(s):
        m = _TOKEN_RE.match(s, pos)
        if not m:
            raise OpggError(f"파싱 실패 위치 {pos}: {s[pos:pos + 40]!r}")
        pos = m.end()
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
    return out


def parse_opgg_text(text):
    """OP.GG MCP 응답 텍스트를 중첩 dict/list로 변환한다."""
    schemas = {}
    body_lines = []
    for line in text.splitlines():
        if line.startswith("class ") and ":" in line:
            name, fields = line[6:].split(":", 1)
            schemas[name.strip()] = [f.strip() for f in fields.split(",") if f.strip()]
        elif line.strip():
            body_lines.append(line)
    if not body_lines:
        raise OpggError(f"응답에 본문이 없음: {text[:200]!r}")
    tokens = _tokenize("".join(body_lines))
    pos = 0

    def value():
        nonlocal pos
        kind, tok = tokens[pos]
        pos += 1
        if kind == "str":
            return json.loads(tok)
        if kind == "num":
            return float(tok) if any(c in tok for c in ".eE") else int(tok)
        if kind == "punct" and tok == "[":
            items = []
            if tokens[pos] == ("punct", "]"):
                pos += 1
                return items
            while True:
                items.append(value())
                kind2, tok2 = tokens[pos]
                pos += 1
                if tok2 == "]":
                    return items
                if tok2 != ",":
                    raise OpggError(f"리스트 구분자 오류: {tok2}")
        if kind == "name":
            if tok in ("null", "None"):
                return None
            if tok in ("true", "True"):
                return True
            if tok in ("false", "False"):
                return False
            if pos < len(tokens) and tokens[pos] == ("punct", "("):
                pos += 1
                args = []
                if tokens[pos] != ("punct", ")"):
                    while True:
                        args.append(value())
                        kind2, tok2 = tokens[pos]
                        pos += 1
                        if tok2 == ")":
                            break
                        if tok2 != ",":
                            raise OpggError(f"인자 구분자 오류: {tok2}")
                else:
                    pos += 1
                fields = schemas.get(tok)
                if fields is None:
                    # 최상위 래퍼(LolGetChampionAnalysis 등)는 스키마 줄이 있음. 없으면 그대로.
                    return args[0] if len(args) == 1 else args
                if len(fields) != len(args):
                    raise OpggError(f"{tok}: 필드 {len(fields)}개, 값 {len(args)}개")
                return dict(zip(fields, args))
            return tok
        raise OpggError(f"예상치 못한 토큰: {tok}")

    return value()
