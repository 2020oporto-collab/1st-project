"""LoLalytics 카운터 페이지에서 '내 라인 챔피언 vs 상대 각 라인 챔피언' 승률을 모은다.

OP.GG에는 다른 라인 상대와의 상성이 없어서 이것만 LoLalytics에서 가져온다.
개인 용도로, 요청 간격을 두고(robots.txt Crawl-delay 1초 이상) 패치당 한 번 정도만 돌린다.

    python -m lol_picker.lolalytics                     # 미드, 정글 (기본)
    python -m lol_picker.lolalytics --positions mid

결과는 data/lolalytics.json 에 따로 저장되고 git에는 올리지 않는다(.gitignore, 재배포 방지).
    lolalytics[포지션][챔피언nk] = {"wr": 내 승률(%), "games": 판수,
                                    "vs": {상대포지션: [[챔피언id, 내 승률(%), 판수, 상대 기준 기대승률(%)], …]}}
"""

import argparse
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request

import os

from .collect import DEFAULT_CACHE, POSITIONS, norm

DEFAULT_OUT = os.path.join(os.path.dirname(DEFAULT_CACHE), "lolalytics.json")

BASE_URL = "https://lolalytics.com/lol/{slug}/counters/?lane={lane}&vslane={vslane}&tier={tier}&patch={patch}"
LANE = {"top": "top", "jungle": "jungle", "mid": "middle", "adc": "bottom", "support": "support"}
SLUG_FIX = {"nunuwillump": "nunu", "renataglasc": "renata"}
USER_AGENT = "Mozilla/5.0 (compatible; lol-picker/1.0; personal use)"


class LolalyticsError(RuntimeError):
    pass


def slug(en_name):
    s = re.sub(r"[^a-z0-9]", "", en_name.lower())
    return SLUG_FIX.get(s, s)


def fetch(url, timeout=40, retries=1):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError:
            raise  # 404 등은 재시도해도 같다
        except (urllib.error.URLError, OSError):
            if attempt == retries:
                raise
            time.sleep(5)


def parse_counters(html):
    """페이지에 박힌 qwik/json 상태에서 (내 통계, 상대 목록)을 꺼낸다.

    qwik 상태는 objs 배열 + base36 인덱스 참조로 되어 있다.
    """
    m = re.search(r'<script type="qwik/json">(.*?)</script>', html, re.S)
    if not m:
        raise LolalyticsError("qwik/json 상태가 없음 (페이지 구조 변경?)")
    objs = json.loads(m.group(1))["objs"]

    def ref(r):
        return objs[int(r, 36)]

    for o in objs:
        if isinstance(o, dict) and "counters" in o and "stats" in o:
            st = ref(o["stats"])
            stats = {k: ref(v) for k, v in st.items() if k in ("cid", "lane", "vsLane", "wr", "avgWr")}
            rows = []
            for r in ref(o["counters"]):
                row = {k: ref(v) for k, v in ref(r).items()}
                rows.append([int(row["cid"]), float(row["vsWr"]), int(row["n"]), float(row["allWr"])])
            return stats, rows
    raise LolalyticsError("counters 데이터를 찾지 못함 (페이지 구조 변경?)")


def collect(cache, store, positions, tier, patch, delay):
    """cache(OP.GG 챔피언/메타)를 참고해 store(lolalytics.json 내용)를 채운다."""
    champs = cache["champions"]
    by_nk = {norm(c["en"]): c for c in champs.values()}
    out = store.setdefault("data", {})
    failures = []
    for pos in positions:
        entries = cache["meta"][pos]
        print(f"[{pos}] 챔피언 {len(entries)}개 × 상대 라인 5개")
        pos_out = {}
        for i, e in enumerate(entries, 1):
            nk = norm(e["champion"])
            c = by_nk.get(nk)
            if not c:
                continue
            item = {"vs": {}}
            for vpos in POSITIONS:
                url = BASE_URL.format(slug=slug(c["en"]), lane=LANE[pos], vslane=LANE[vpos],
                                      tier=tier, patch=patch)
                try:
                    stats, rows = parse_counters(fetch(url))
                    item["wr"] = float(stats["wr"])
                    item["vs"][vpos] = rows
                except (urllib.error.URLError, LolalyticsError, ValueError) as err:
                    failures.append(f"{c['en']} {pos} vs {vpos}: {err}")
                time.sleep(delay)
            if item["vs"]:
                pos_out[nk] = item
            if i % 10 == 0:
                print(f"  {i}/{len(entries)}")
        out[pos] = pos_out
    store["meta"] = {
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "tier": tier, "patch": patch, "positions": sorted(out),
    }
    return failures


def main(argv=None):
    ap = argparse.ArgumentParser(description="LoLalytics 교차 라인 상성 수집")
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="OP.GG 캐시 (챔피언 목록/메타)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--positions", nargs="+", default=["mid", "jungle"], choices=POSITIONS)
    ap.add_argument("--tier", default="emerald_plus")
    ap.add_argument("--patch", default="30", help="30 = 최근 30일 (기본), 또는 16.20 같은 패치 번호")
    ap.add_argument("--delay", type=float, default=1.5, help="요청 간격(초), 1 이상")
    a = ap.parse_args(argv)
    if a.delay < 1:
        ap.error("--delay 는 1초 이상이어야 합니다 (robots.txt Crawl-delay)")
    with open(a.cache, encoding="utf-8") as f:
        cache = json.load(f)
    store = {}
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            store = json.load(f)
    failures = collect(cache, store, a.positions, a.tier, a.patch, a.delay)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False)
    print(f"저장 완료: {a.out}")
    if failures:
        print(f"실패 {len(failures)}건:", file=sys.stderr)
        for x in failures[:30]:
            print("  " + x, file=sys.stderr)


if __name__ == "__main__":
    main()
