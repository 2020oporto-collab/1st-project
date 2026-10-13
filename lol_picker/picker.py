"""상대/아군 픽을 넣으면 내 포지션의 최적 챔피언을 추천한다.

    python -m lol_picker.picker --me mid --enemy 탑=신지드 정글=오공
    python -m lol_picker.picker --me mid --enemy mid=카사딘 --ally adc=케이틀린 --ban 제드

점수는 로그오즈(logit) 위에서 더한다.
  기본 승률(포지션별 OP.GG 통계) + 맞라인 상성 + 아군 시너지 + 추정 규칙 + 내 숙련도
'추정' 표시가 붙은 항목은 통계가 아니라 규칙 기반 추정치다.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys

from .collect import DEFAULT_CACHE, POSITIONS, SYNERGY_POSITIONS, norm

# 표본 보정 강도: 관측 판수가 이 값만큼 '기대값' 쪽으로 당겨진다.
# 상성/시너지는 OP.GG가 '가장 극단적인 상위 3개'만 주므로(선택 편향) 보정을 세게 건다.
K_BASE = 3000
K_MATCHUP = 300
K_SYNERGY = 500
SYNERGY_WEIGHT = 0.5
K_MY_CHAMP = 20
MIN_RECENT_GAMES = 2  # 시즌 상위 챔피언이 아니면 최근 20판에서 이 이상 해야 챔피언풀로 본다
MY_CHAMP_WEIGHT = 0.5
# LoLalytics 상대 라인별 전적 (전체 목록이라 선택 편향 없음)
K_LOLA = 300
CROSS_LANE_WEIGHT = 0.7  # 다른 라인 효과는 서로 겹치는 부분이 있어 줄여서 더한다
HEURISTIC_STEP = 0.04  # 로그오즈 0.04 ≈ 승률 1%p
MIN_META_PLAY = 1000   # 이보다 적게 플레이된 챔피언은 메타 후보에서 뺀다
STALE_DAYS = 7

POSITION_ALIASES = {
    "top": "top", "탑": "top",
    "jungle": "jungle", "jg": "jungle", "jug": "jungle", "정글": "jungle",
    "mid": "mid", "middle": "mid", "미드": "mid",
    "adc": "adc", "bot": "adc", "bottom": "adc", "원딜": "adc", "바텀": "adc", "봇": "adc",
    "support": "support", "sup": "support", "supp": "support", "서폿": "support", "서포터": "support",
}
POSITION_KO = {"top": "탑", "jungle": "정글", "mid": "미드", "adc": "원딜", "support": "서포터"}


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(x):
    return 1 / (1 + math.exp(-x))


def shrink(win, play, expected, k):
    """관측 승률을 기대 승률 쪽으로 k판만큼 당긴다(표본이 작을수록 크게)."""
    return (win + k * expected) / (play + k)


class PickerError(ValueError):
    pass


class Picker:
    def __init__(self, cache):
        self.cache = cache
        self.champs = cache["champions"]
        self.analysis = cache.get("analysis", {})
        self.meta = cache.get("meta", {})
        self.profile = cache.get("profile") or {}
        self.lola = cache.get("lolalytics", {})  # load_cache()가 lolalytics.json을 합쳐 넣는다

        # 이름 → 정규화된 영문 이름(nk). 모든 데이터는 nk로 찾는다.
        self.alias = {}
        self.by_nk = {}
        for c in self.champs.values():
            nk = norm(c["en"])
            self.by_nk[nk] = c
            for name in (c["en"], c["ko"], c["key"]):
                self.alias.setdefault(norm(name), nk)
        self.nk_by_id = {int(c["id"]): norm(c["en"]) for c in self.champs.values()}
        self._lola_index = {}
        self.base = {
            p: {norm(e["champion"]): (e["win"], e["play"]) for e in self.meta.get(p, [])}
            for p in POSITIONS
        }

    # ---- 이름/포지션 ----------------------------------------------------
    def resolve(self, name):
        key = norm(name)
        if key in self.alias:
            return self.alias[key]
        hits = sorted({nk for a, nk in self.alias.items() if key and key in a})
        if len(hits) == 1:
            return hits[0]
        hint = ", ".join(self.display(h) for h in hits[:6])
        raise PickerError(f"챔피언 '{name}'을(를) 찾을 수 없음" + (f" (혹시: {hint})" if hint else ""))

    @staticmethod
    def resolve_position(pos):
        p = POSITION_ALIASES.get(pos.strip().lower())
        if not p:
            raise PickerError(f"포지션 '{pos}'을(를) 알 수 없음 (탑/정글/미드/원딜/서폿)")
        return p

    def display(self, nk):
        c = self.by_nk.get(nk)
        return c["ko"] if c else nk

    # ---- 통계 조회 ------------------------------------------------------
    def base_wr(self, pos, nk):
        """포지션 승률. 판수가 적은 비주류 픽(장인 위주라 승률이 부풀기 쉬움)은 50% 쪽으로 보정."""
        wp = self.base.get(pos, {}).get(nk)
        if not wp or wp[1] == 0:
            return None, 0
        return shrink(wp[0], wp[1], 0.5, K_BASE), wp[1]

    def _an(self, pos, nk):
        return self.analysis.get(pos, {}).get(nk)

    def matchup_record(self, pos, me, enemy):
        """같은 라인 상대전적 (내 승, 판수). 양쪽 챔피언의 카운터 목록을 모두 본다."""
        a = self._an(pos, me)
        if a:
            for x in a["counters"]:
                if norm(x["champion_name"]) == enemy:
                    return x["win"], x["play"]
        b = self._an(pos, enemy)
        if b:
            for x in b["counters"]:
                if norm(x["champion_name"]) == me:
                    return x["play"] - x["win"], x["play"]
        return None

    def synergy_record(self, pos, me, ally_pos, ally):
        """아군 조합 전적 (승, 판수). 양쪽의 시너지 목록을 모두 본다."""
        a = self._an(pos, me)
        if a and ally_pos in SYNERGY_POSITIONS:
            for x in a["synergies"].get(ally_pos, []):
                if norm(x["synergy_champion_name"]) == ally:
                    return x["win"], x["play"]
        b = self._an(ally_pos, ally)
        if b and pos in SYNERGY_POSITIONS:
            for x in b["synergies"].get(pos, []):
                if norm(x["synergy_champion_name"]) == me:
                    return x["win"], x["play"]
        return None

    @staticmethod
    def _lola_expected(my_wr, all_wr):
        """상대 강도만으로 본 기대 승률. all_wr = 100 - 상대 승률(%)."""
        return sigmoid(logit(my_wr) + logit(all_wr / 100))

    def lola_record(self, pos, me, vpos, enemy):
        """LoLalytics: 내(pos) vs 상대(vpos) 전적 → (내 승률, 판수, 기대 승률, 중심화 보정값).

        LoLalytics는 챔피언 평균 승률이 50%가 아니라(약 51.8%) 기대값과의 차이가 전부 +쪽으로
        치우친다. 그래서 상대 목록 전체의 판수 가중 평균 차이를 0으로 맞추는 보정값(offset)을 함께 준다.
        """
        item = self.lola.get(pos, {}).get(me)
        if not item or vpos not in item.get("vs", {}):
            return None
        key = (pos, me, vpos)
        if key not in self._lola_index:
            rows = item["vs"][vpos]
            my_wr = item["wr"] / 100
            total = sum(r[2] for r in rows)
            offset = sum(r[2] * (logit(r[1] / 100) - logit(self._lola_expected(my_wr, r[3])))
                         for r in rows) / total if total else 0.0
            self._lola_index[key] = ({self.nk_by_id.get(r[0]): r for r in rows}, offset)
        index, offset = self._lola_index[key]
        r = index.get(enemy)
        if not r:
            return None
        _, vs_wr, n, all_wr = r
        expected = sigmoid(logit(self._lola_expected(item["wr"] / 100, all_wr)) + offset)
        return vs_wr / 100, n, expected

    def has_lola(self, pos, me):
        return bool(self.lola.get(pos, {}).get(me, {}).get("vs"))

    def damage_type(self, nk, pos=None):
        for p in ([pos] if pos else []) + POSITIONS:
            a = self._an(p, nk) if p else None
            if a and a.get("damage_type"):
                return a["damage_type"]
        info = (self.by_nk.get(nk) or {}).get("info") or {}
        if info:
            return "AP" if info.get("magic", 0) > info.get("attack", 0) else "AD"
        return None

    def primary_tag(self, nk):
        tags = (self.by_nk.get(nk) or {}).get("tags") or []
        return tags[0] if tags else None

    # ---- 내 챔피언풀 ----------------------------------------------------
    def my_record(self, nk):
        for x in self.profile.get("season_champions", []):
            if self.alias.get(norm(x["champion_name"])) == nk:
                return x["win"], x["play"], "시즌"
        recent = [m for m in self.profile.get("recent_matches", [])
                  if self.alias.get(norm(m["champion"])) == nk]
        if recent:
            return sum(m["result"] == "WIN" for m in recent), len(recent), "최근 20판"
        return None

    def my_pool(self, pos):
        """이 포지션에서 내가 쓰는 챔피언: 최근 이 포지션으로 했거나, 주 포지션이 이곳인 시즌 챔피언."""
        pool = set()
        recent = {}
        for m in self.profile.get("recent_matches", []):
            nk = self.alias.get(norm(m["champion"]))
            if nk and m["position"] == pos:
                recent[nk] = recent.get(nk, 0) + 1
        season = {self.alias.get(norm(x["champion_name"])) for x in self.profile.get("season_champions", [])}
        for nk, n in recent.items():
            if n >= MIN_RECENT_GAMES or nk in season:
                pool.add(nk)
        for x in self.profile.get("season_champions", []):
            nk = self.alias.get(norm(x["champion_name"]))
            if not nk:
                continue
            plays = {p: self.base[p].get(nk, (0, 0))[1] for p in POSITIONS}
            if max(plays.values()) > 0 and max(plays, key=plays.get) == pos:
                pool.add(nk)
        return pool

    # ---- 점수 -----------------------------------------------------------
    def score(self, pos, cand, enemies, allies, use_my_record=False):
        """enemies/allies: {포지션: nk}. 반환: (승률, 항목 목록, 경고 목록)"""
        factors, notes = [], []
        bwr, bplay = self.base_wr(pos, cand)
        if bwr is None:
            raise PickerError(f"{self.display(cand)}: {POSITION_KO[pos]} 통계 없음")
        x = logit(bwr)
        factors.append(("기본 승률", bwr, "통계", f"{POSITION_KO[pos]} {bplay:,}판"))
        if self._an(pos, cand) is None:
            notes.append("상성/시너지 데이터 없음")

        def add(label, delta, kind, detail):
            nonlocal x
            x += delta
            factors.append((label, delta, kind, detail))

        # 1) 상대 픽과의 상성 (LoLalytics: 모든 라인 / 없으면 OP.GG: 같은 라인만)
        lola_ok = self.has_lola(pos, cand)
        for vpos, enemy in enemies.items():
            rec = self.lola_record(pos, cand, vpos, enemy) if lola_ok else None
            if not rec:
                continue
            vs_wr, n, expected = rec
            p = shrink(vs_wr * n, n, expected, K_LOLA)
            inter = logit(p) - logit(expected)
            if vpos == pos:
                owr, _ = self.base_wr(pos, enemy)
                strength = -logit(owr) if owr else 0.0
                add(f"맞라인 vs {self.display(enemy)}", strength + inter, "통계",
                    f"LoLalytics {n:,}판 실제 {vs_wr:.1%} / 기대 {expected:.1%}")
            else:
                add(f"상대 {POSITION_KO[vpos]} {self.display(enemy)}", CROSS_LANE_WEIGHT * inter, "통계",
                    f"LoLalytics {n:,}판 실제 {vs_wr:.1%} / 기대 {expected:.1%}")
        if lola_ok:
            missing = [e for vp, e in enemies.items() if not self.lola_record(pos, cand, vp, e)]
            if missing:
                notes.append("LoLalytics 표본 없음: " + ", ".join(self.display(e) for e in missing))

        opp = enemies.get(pos)
        if opp and not (lola_ok and self.lola_record(pos, cand, pos, opp)):
            owr, _ = self.base_wr(pos, opp)
            expected = sigmoid(logit(bwr) - logit(owr)) if owr else bwr
            rec = self.matchup_record(pos, cand, opp)
            if rec:
                w, n = rec
                p = shrink(w, n, expected, K_MATCHUP)
                add(f"맞라인 vs {self.display(opp)}", logit(p) - logit(bwr), "통계",
                    f"{w}/{n}판 실제 {w / n:.1%}")
            else:
                add(f"맞라인 vs {self.display(opp)}", logit(expected) - logit(bwr), "통계",
                    "상성 표본 없음 → 상대 기본 승률만 반영")

        # 2) 아군 시너지
        for apos, ally in allies.items():
            rec = self.synergy_record(pos, cand, apos, ally)
            if not rec:
                continue
            awr, _ = self.base_wr(apos, ally)
            awr = awr or 0.5
            expected = sigmoid(logit(bwr) + logit(awr))
            w, n = rec
            p = shrink(w, n, expected, K_SYNERGY)
            add(f"시너지 {POSITION_KO[apos]} {self.display(ally)}",
                SYNERGY_WEIGHT * (logit(p) - logit(expected)), "통계",
                f"{w}/{n}판 실제 {w / n:.1%}")

        # 3) 추정 규칙 (다른 라인 상대 / 팀 조합) — 통계 아님
        my_dt = self.damage_type(cand, pos)
        my_tag = self.primary_tag(cand)
        ally_dts = [self.damage_type(a, p) for p, a in allies.items()]
        if len(ally_dts) >= 2 and my_dt in ("AD", "AP"):
            n_ap = sum(d == "AP" for d in ally_dts) + 0.5 * sum(d == "BOTH" for d in ally_dts)
            n_ad = sum(d == "AD" for d in ally_dts) + 0.5 * sum(d == "BOTH" for d in ally_dts)
            if n_ap == 0:
                add("팀 딜 밸런스", HEURISTIC_STEP if my_dt == "AP" else -HEURISTIC_STEP, "추정",
                    "아군에 AP 없음")
            elif n_ad == 0:
                add("팀 딜 밸런스", HEURISTIC_STEP if my_dt == "AD" else -HEURISTIC_STEP, "추정",
                    "아군에 AD 없음")
        enemy_tags = [self.primary_tag(e) for e in enemies.values()]
        if my_tag == "Assassin" and not lola_ok:  # 실제 교차 라인 통계가 있으면 이 추정은 생략
            n_tank = enemy_tags.count("Tank")
            n_squishy = sum(t in ("Mage", "Marksman", "Assassin") for t in enemy_tags)
            if n_tank >= 2:
                add("적 탱커 다수", -HEURISTIC_STEP, "추정", f"적 탱커 {n_tank}명 → 암살자 불리")
            elif n_squishy >= 3:
                add("적 물몸 다수", HEURISTIC_STEP, "추정", f"적 물몸 {n_squishy}명 → 암살자 유리")

        # 4) 내 숙련도
        if use_my_record:
            rec = self.my_record(cand)
            if rec:
                w, n, src = rec
                p = shrink(w, n, 0.5, K_MY_CHAMP)
                add("내 숙련도", MY_CHAMP_WEIGHT * logit(p), "내 전적", f"{src} {w}승 {n - w}패")

        return sigmoid(x), factors, notes

    def recommend(self, pos, enemies, allies, bans=(), top=3):
        taken = set(enemies.values()) | set(allies.values()) | set(bans)
        pool = self.my_pool(pos) - taken
        meta = {nk for nk, (w, n) in self.base[pos].items() if n >= MIN_META_PLAY} - taken
        meta_rank = sorted(((self.score(pos, nk, enemies, allies) + (nk,)) for nk in meta),
                           key=lambda r: -r[0])
        pool_rank = sorted(((self.score(pos, nk, enemies, allies, use_my_record=True) + (nk,))
                            for nk in pool if self.base_wr(pos, nk)[0] is not None),
                           key=lambda r: -r[0])
        return pool_rank[:top], meta_rank[:top]


# ---- 출력 ---------------------------------------------------------------

def fmt_delta(delta, base_p):
    """로그오즈 변화량을 '대략 몇 %p'로 표시."""
    pp = delta * base_p * (1 - base_p) * 100
    return f"{pp:+.1f}%p"


def render(picker, pos, enemies, allies, pool_rank, meta_rank):
    lines = []
    head = [f"내 포지션: {POSITION_KO[pos]}"]
    if enemies:
        head.append("상대: " + ", ".join(f"{POSITION_KO[p]} {picker.display(c)}" for p, c in enemies.items()))
    if allies:
        head.append("아군: " + ", ".join(f"{POSITION_KO[p]} {picker.display(c)}" for p, c in allies.items()))
    lines.append(" | ".join(head))

    for title, rank in (("내 챔피언풀 추천", pool_rank), ("전체 통계 추천", meta_rank)):
        lines.append("")
        lines.append(f"■ {title}")
        if not rank:
            lines.append("  (후보 없음)")
            continue
        for i, (p, factors, notes, nk) in enumerate(rank, 1):
            lines.append(f"  {i}. {picker.display(nk)}  예상 승률 {p:.1%}")
            base_p = factors[0][1]
            for label, val, kind, detail in factors:
                if label == "기본 승률":
                    lines.append(f"     - {label} {val:.1%} ({detail}, 표본 보정 후)")
                else:
                    tag = " [추정]" if kind == "추정" else ""
                    lines.append(f"     - {label} {fmt_delta(val, base_p)}{tag} ({detail})")
            for n in notes:
                lines.append(f"     ! {n}")
    lines.append("")
    if picker.lola.get(pos):
        meta = picker.cache.get("lolalytics_meta", {})
        period = "최근 30일" if str(meta.get("patch")) == "30" else f"패치 {meta.get('patch')}"
        lines.append(f"※ 상대 픽 상성: LoLalytics ({meta.get('tier', '')}, {period}) / 기본 승률·시너지: OP.GG")
    else:
        lines.append("※ 이 포지션은 LoLalytics 데이터가 없어 다른 라인 상대 상성이 빠져 있습니다.")
    lines.append("※ [추정]은 통계가 아니라 규칙 기반 보정입니다.")
    return "\n".join(lines)


def parse_picks(picker, items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise PickerError(f"'{it}' 형식 오류 — 포지션=챔피언 (예: 탑=신지드)")
        p, c = it.split("=", 1)
        pos = picker.resolve_position(p)
        if pos in out:
            raise PickerError(f"{POSITION_KO[pos]} 포지션이 두 번 입력됨")
        out[pos] = picker.resolve(c)
    return out


def load_cache(path):
    if not os.path.exists(path):
        raise PickerError(f"캐시 파일이 없습니다: {path}\n먼저 `python -m lol_picker.collect`를 실행하세요.")
    with open(path, encoding="utf-8") as f:
        cache = json.load(f)
    lola_path = os.path.join(os.path.dirname(path), "lolalytics.json")
    if os.path.exists(lola_path):
        with open(lola_path, encoding="utf-8") as f:
            lola = json.load(f)
        cache["lolalytics"] = lola.get("data", {})
        cache["lolalytics_meta"] = lola.get("meta", {})
    return cache


def stale_warning(cache):
    ts = cache.get("collected_at")
    if not ts:
        return None
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(ts)
    if age.days >= STALE_DAYS:
        return f"⚠ 통계가 {age.days}일 전 데이터입니다. `python -m lol_picker.collect`로 갱신하세요."
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="상대 픽 기반 챔피언 추천")
    ap.add_argument("--me", required=True, help="내 포지션 (탑/정글/미드/원딜/서폿)")
    ap.add_argument("--enemy", nargs="*", default=[], help="상대 픽: 포지션=챔피언 …")
    ap.add_argument("--ally", nargs="*", default=[], help="아군 픽: 포지션=챔피언 …")
    ap.add_argument("--ban", nargs="*", default=[], help="밴된 챔피언")
    ap.add_argument("--top", type=int, default=3, help="각 목록에서 보여줄 개수")
    ap.add_argument("--cache", default=DEFAULT_CACHE)
    a = ap.parse_args(argv)
    try:
        cache = load_cache(a.cache)
        picker = Picker(cache)
        pos = picker.resolve_position(a.me)
        enemies = parse_picks(picker, a.enemy)
        allies = parse_picks(picker, a.ally)
        if pos in allies:
            raise PickerError("아군 픽에 내 포지션이 들어 있습니다")
        bans = [picker.resolve(b) for b in a.ban]
        pool_rank, meta_rank = picker.recommend(pos, enemies, allies, bans, a.top)
    except PickerError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    warn = stale_warning(cache)
    if warn:
        print(warn)
    print(render(picker, pos, enemies, allies, pool_rank, meta_rank))
    return 0


if __name__ == "__main__":
    sys.exit(main())
