"""OP.GG 데이터를 모아 data/cache.json 으로 저장한다.

    python -m lol_picker.collect                       # 전체 수집 (5개 포지션, 수 분 소요)
    python -m lol_picker.collect --positions mid jungle
    python -m lol_picker.collect --only-profile        # 내 전적만 갱신
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

from .opgg_client import OpggClient, OpggError

POSITIONS = ["top", "jungle", "mid", "adc", "support"]
SYNERGY_POSITIONS = ["jungle", "mid", "adc", "support"]  # OP.GG 분석 API가 주는 시너지 라인
DEFAULT_CACHE = os.path.join(os.path.dirname(__file__), "data", "cache.json")
DEFAULT_SUMMONER = ("기계꽃", "KR1", "KR")


def norm(name):
    """챔피언 이름 비교용 정규화: 영문/숫자만 남기고 대문자. "Kha'Zix" -> "KHAZIX"."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", name or "").upper()


def api_name(en_name):
    """API용 이름: "Twisted Fate" -> "TWISTED_FATE", "Kha'Zix" -> "KHAZIX"."""
    return re.sub(r"[^0-9A-Za-z]+", "_", en_name.replace("'", "")).strip("_").upper()


def collect_champions(c):
    fields = ["data.champions[].{champion_id,key,name}"]
    en = c.call("lol_list_champions", {"lang": "en_US", "desired_output_fields": fields})
    ko = c.call("lol_list_champions", {"lang": "ko_KR", "desired_output_fields": fields})
    ko_by_id = {x["champion_id"]: x["name"] for x in ko["data"]["champions"]}
    champs = {}
    for x in en["data"]["champions"]:
        if x["champion_id"] >= 10000:  # 특수 모드 변형(Jade_Annie 등)은 같은 이름이라 제외
            continue
        champs[str(x["champion_id"])] = {
            "id": x["champion_id"],
            "key": x["key"],
            "en": x["name"],
            "ko": ko_by_id.get(x["champion_id"], x["name"]),
        }
    return champs


def collect_details(c, champs):
    """태그(Mage/Tank…)와 공격/마법 수치. 교차 라인 추정 규칙에 쓴다."""
    def fetch(batch):
        r = c.call("lol_list_champion_details", {
            "champions": [api_name(champs[k]["en"]) for k in batch],
            "desired_output_fields": [
                "data.champions[].{key,tags[]}",
                "data.champions[].info.{attack,magic,defense}",
            ],
        })
        by_key = {x["key"]: x for x in r["data"]["champions"]}
        for k in batch:
            d = by_key.get(champs[k]["key"])
            if d:
                champs[k]["tags"] = d.get("tags") or []
                champs[k]["info"] = d.get("info") or {}

    ids = list(champs)
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        try:
            fetch(batch)
        except OpggError:
            # 한 챔피언 이름이 틀리면 묶음 전체가 실패하므로 하나씩 다시 시도
            for k in batch:
                try:
                    fetch([k])
                except OpggError as e:
                    print(f"  상세정보 실패 {champs[k]['en']}: {e}", file=sys.stderr)


def collect_meta(c):
    fields = [f"data.positions.{p}[].{{champion,play,win,tier,rank,pick_rate,ban_rate,role_rate}}"
              for p in POSITIONS]
    r = c.call("lol_list_lane_meta_champions", {"position": "all", "desired_output_fields": fields})
    return {p: r["data"]["positions"].get(p) or [] for p in POSITIONS}


def collect_analysis(c, en_name, position, tier):
    fields = [
        "data.damage_type",
        "data.strong_counters[].{champion_name,play,win}",
        "data.weak_counters[].{champion_name,play,win}",
    ] + [f"data.synergies.{p}[].{{synergy_champion_name,play,win}}" for p in SYNERGY_POSITIONS]
    args = {
        "champion": api_name(en_name),
        "position": position,
        "game_mode": "ranked",
        "desired_output_fields": fields,
    }
    if tier and tier != "all":
        args["tier"] = tier
    d = c.call("lol_get_champion_analysis", args).get("data") or {}
    syn = d.get("synergies") or {}
    return {
        "damage_type": d.get("damage_type"),
        "counters": (d.get("strong_counters") or []) + (d.get("weak_counters") or []),
        "synergies": {p: syn.get(p) or [] for p in SYNERGY_POSITIONS},
    }


def collect_profile(c, game_name, tag_line, region):
    prof = c.call("lol_get_summoner_profile", {
        "game_name": game_name, "tag_line": tag_line, "region": region,
        "desired_output_fields": [
            "data.summoner.most_champions.champion_stats[].{champion_name,play,win}",
            "data.summoner.league_stats[].tier_info.{tier,division,lp}",
            "data.summoner.league_stats[].game_type",
        ],
    })
    matches = c.call("lol_list_summoner_matches", {
        "game_name": game_name, "tag_line": tag_line, "region": region, "limit": 20,
        "desired_output_fields": [
            "data.game_history[].participants[].{champion_name,position}",
            "data.game_history[].participants[].stats.result",
        ],
    })
    recent = []
    for g in matches["data"]["game_history"]:
        p = g["participants"][0]
        recent.append({"champion": p["champion_name"], "position": (p.get("position") or "").lower(),
                       "result": p["stats"]["result"]})
    return {
        "summoner": f"{game_name}#{tag_line}",
        "region": region,
        "league": prof["data"]["summoner"].get("league_stats"),
        "season_champions": prof["data"]["summoner"]["most_champions"]["champion_stats"],
        "recent_matches": recent,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="OP.GG 데이터 수집")
    ap.add_argument("--out", default=DEFAULT_CACHE)
    ap.add_argument("--tier", default="emerald_plus")
    ap.add_argument("--positions", nargs="+", default=POSITIONS, choices=POSITIONS)
    ap.add_argument("--summoner", default=f"{DEFAULT_SUMMONER[0]}#{DEFAULT_SUMMONER[1]}")
    ap.add_argument("--region", default=DEFAULT_SUMMONER[2])
    ap.add_argument("--only-profile", action="store_true")
    a = ap.parse_args(argv)

    game_name, tag_line = a.summoner.split("#", 1)
    c = OpggClient()
    cache = {}
    if os.path.exists(a.out):
        with open(a.out, encoding="utf-8") as f:
            cache = json.load(f)

    print("내 전적 수집…")
    cache["profile"] = collect_profile(c, game_name, tag_line, a.region)

    if not a.only_profile:
        print("챔피언 목록/상세정보 수집…")
        champs = collect_champions(c)
        collect_details(c, champs)
        cache["champions"] = champs
        print("포지션별 메타 수집…")
        cache["meta"] = collect_meta(c)
        cache["tier"] = a.tier
        analyses = cache.get("analysis", {}) if cache.get("tier") == a.tier else {}
        for pos in a.positions:
            entries = cache["meta"][pos]
            print(f"[{pos}] 챔피언 {len(entries)}개 분석 수집…")
            pos_data = {}
            for i, e in enumerate(entries, 1):
                try:
                    pos_data[norm(e["champion"])] = collect_analysis(c, e["champion"], pos, a.tier)
                except OpggError as err:
                    print(f"  {e['champion']} 실패: {err}", file=sys.stderr)
                if i % 10 == 0:
                    print(f"  {i}/{len(entries)}")
            analyses[pos] = pos_data
        cache["analysis"] = analyses
        cache["collected_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    cache["profile_collected_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    print(f"저장 완료: {a.out}")


if __name__ == "__main__":
    main()
