import unittest

import json

from lol_picker.collect import api_name, norm
from lol_picker.lolalytics import LolalyticsError, parse_counters, slug
from lol_picker.opgg_client import parse_opgg_text
from lol_picker.picker import Picker, PickerError, parse_picks


SAMPLE = '''class LolGetChampionAnalysis: data
class Data: summary,damage_type,weak_counters
class Summary: average_stats
class AverageStats: play,win_rate
class WeakCounter: champion_name,play,win

LolGetChampionAnalysis(Data(Summary(AverageStats(64306,0.5)),"AP",[WeakCounter("Talon",319,133),WeakCounter("Kai'Sa",327,141)]))'''


def champ(cid, en, ko, tags, attack, magic):
    return {"id": cid, "key": en.replace(" ", "").replace("'", ""), "en": en, "ko": ko,
            "tags": tags, "info": {"attack": attack, "magic": magic}}


def make_cache():
    champs = [
        champ(1, "Malzahar", "말자하", ["Mage"], 2, 9),
        champ(2, "Talon", "탈론", ["Assassin"], 9, 1),
        champ(3, "Annie", "애니", ["Mage"], 2, 10),
        champ(4, "Singed", "신지드", ["Tank", "Mage"], 4, 7),
        champ(5, "Wukong", "오공", ["Fighter", "Tank"], 8, 2),
        champ(6, "Kai'Sa", "카이사", ["Marksman"], 8, 3),
        champ(7, "Zed", "제드", ["Assassin"], 9, 1),
        champ(8, "Malphite", "말파이트", ["Tank", "Fighter"], 5, 7),
    ]
    meta = {
        "top": [{"champion": "Singed", "play": 20000, "win": 10200},
                {"champion": "Malphite", "play": 20000, "win": 10000}],
        "jungle": [{"champion": "Wukong", "play": 30000, "win": 15000}],
        "mid": [{"champion": "Malzahar", "play": 50000, "win": 25000},
                {"champion": "Talon", "play": 20000, "win": 10400},
                {"champion": "Annie", "play": 20000, "win": 10200},
                {"champion": "Zed", "play": 40000, "win": 20000}],
        "adc": [{"champion": "Kai'Sa", "play": 80000, "win": 40000}],
        "support": [],
    }
    analysis = {
        "mid": {
            "MALZAHAR": {"damage_type": "AP",
                         "counters": [{"champion_name": "Talon", "play": 400, "win": 160}],
                         "synergies": {"jungle": [{"synergy_champion_name": "Wukong", "play": 300, "win": 180}],
                                       "mid": [], "adc": [], "support": []}},
            "ANNIE": {"damage_type": "AP",
                      "counters": [{"champion_name": "Malzahar", "play": 300, "win": 180}],
                      "synergies": {"jungle": [], "mid": [], "adc": [], "support": []}},
            "ZED": {"damage_type": "AD", "counters": [],
                    "synergies": {"jungle": [], "mid": [], "adc": [], "support": []}},
        }
    }
    profile = {
        "season_champions": [{"champion_name": "Malzahar", "play": 66, "win": 39}],
        "recent_matches": [{"champion": "Talon", "position": "mid", "result": "WIN"},
                           {"champion": "Talon", "position": "mid", "result": "LOSE"},
                           {"champion": "Zed", "position": "mid", "result": "WIN"}],
    }
    return {"champions": {str(c["id"]): c for c in champs}, "meta": meta,
            "analysis": analysis, "profile": profile}


class ParserTest(unittest.TestCase):
    def test_parse(self):
        d = parse_opgg_text(SAMPLE)
        self.assertEqual(d["data"]["summary"]["average_stats"]["play"], 64306)
        self.assertEqual(d["data"]["damage_type"], "AP")
        self.assertEqual(d["data"]["weak_counters"][1]["champion_name"], "Kai'Sa")

    def test_null_and_empty_list(self):
        d = parse_opgg_text("class A: x,y,z\n\nW(A(null,[],-1.5))")
        self.assertEqual(d, {"x": None, "y": [], "z": -1.5})

    def test_names(self):
        self.assertEqual(api_name("Kha'Zix"), "KHAZIX")
        self.assertEqual(api_name("Twisted Fate"), "TWISTED_FATE")
        self.assertEqual(api_name("Dr. Mundo"), "DR_MUNDO")
        self.assertEqual(norm("Kai'Sa"), "KAISA")


class PickerTest(unittest.TestCase):
    def setUp(self):
        self.p = Picker(make_cache())

    def test_resolve_korean_and_english(self):
        self.assertEqual(self.p.resolve("오공"), "WUKONG")
        self.assertEqual(self.p.resolve("kaisa"), "KAISA")
        self.assertEqual(self.p.resolve_position("서폿"), "support")
        with self.assertRaises(PickerError):
            self.p.resolve("없는챔프")

    def test_matchup_both_directions(self):
        # 말자하 목록에서 직접
        self.assertEqual(self.p.matchup_record("mid", "MALZAHAR", "TALON"), (160, 400))
        # 애니 목록을 뒤집어서: 애니가 말자하 상대로 180/300 → 말자하 120/300
        self.assertEqual(self.p.matchup_record("mid", "MALZAHAR", "ANNIE"), (120, 300))
        self.assertIsNone(self.p.matchup_record("mid", "MALZAHAR", "ZED"))

    def test_bad_matchup_lowers_score(self):
        base, _, _ = self.p.score("mid", "MALZAHAR", {}, {})
        talon_base, _ = self.p.base_wr("mid", "TALON")
        self.assertLess(talon_base, 10400 / 20000)  # 판수 보정으로 52%보다 낮아짐
        vs_talon, factors, _ = self.p.score("mid", "MALZAHAR", {"mid": "TALON"}, {})
        self.assertAlmostEqual(base, 0.5, places=3)
        self.assertLess(vs_talon, base)
        # 표본 보정 때문에 실제 40%보다는 덜 떨어진다
        self.assertGreater(vs_talon, 0.40)
        self.assertTrue(any(f[0].startswith("맞라인") for f in factors))

    def test_synergy_uses_ally(self):
        with_wk, factors, _ = self.p.score("mid", "MALZAHAR", {}, {"jungle": "WUKONG"})
        self.assertGreater(with_wk, 0.5)
        self.assertTrue(any(f[0].startswith("시너지") for f in factors))

    def test_heuristic_tanks_penalize_assassin(self):
        enemies = {"top": "SINGED", "support": "MALPHITE"}
        _, factors, _ = self.p.score("mid", "ZED", enemies, {})
        self.assertTrue(any(f[0] == "적 탱커 다수" and f[2] == "추정" for f in factors))

    def test_recommend_excludes_taken_and_splits_pool(self):
        pool, meta = self.p.recommend("mid", {"mid": "ANNIE"}, {}, bans=["ZED"], top=10)
        meta_names = [r[3] for r in meta]
        self.assertNotIn("ANNIE", meta_names)
        self.assertNotIn("ZED", meta_names)
        # 탈론: 최근 2판 → 포함 / 제드: 최근 1판뿐 + 밴 → 제외
        self.assertEqual({r[3] for r in pool}, {"MALZAHAR", "TALON"})
        pool2, _ = self.p.recommend("mid", {}, {}, top=10)
        self.assertNotIn("ZED", {r[3] for r in pool2})
        self.assertTrue(any(f[0] == "내 숙련도" for f in pool[0][1]))

    def test_parse_picks(self):
        self.assertEqual(parse_picks(self.p, ["탑=신지드", "정글=오공"]),
                         {"top": "SINGED", "jungle": "WUKONG"})
        with self.assertRaises(PickerError):
            parse_picks(self.p, ["신지드"])


def qwik_page(objs):
    return f'<html><script type="qwik/json">{json.dumps({"objs": objs})}</script></html>'


class LolalyticsTest(unittest.TestCase):
    def test_parse_counters(self):
        # 참조는 base36 인덱스: "a" = 10
        objs = [
            {"stats": "1", "counters": "4"},           # 0
            {"cid": "2", "wr": "3", "counters": "4"},   # 1
            90, "51.95",                                # 2, 3
            ["5"],                                      # 4
            {"cid": "6", "vsWr": "7", "n": "8", "d1": "9", "allWr": "a"},  # 5
            2, 44.63, 177, -3.38, 48.01,                # 6..10
        ]
        stats, rows = parse_counters(qwik_page(objs))
        self.assertEqual(stats["cid"], 90)
        self.assertEqual(stats["wr"], "51.95")
        self.assertEqual(rows, [[2, 44.63, 177, 48.01]])

    def test_parse_fails_loudly(self):
        with self.assertRaises(LolalyticsError):
            parse_counters("<html>no data</html>")

    def test_slug(self):
        self.assertEqual(slug("Twisted Fate"), "twistedfate")
        self.assertEqual(slug("Nunu & Willump"), "nunu")
        self.assertEqual(slug("Kha'Zix"), "khazix")


class LolalyticsScoreTest(unittest.TestCase):
    def setUp(self):
        cache = make_cache()
        # 말자하(미드) vs 상대 탑 신지드: 1000판 40% (기대 ~50%) → 불리
        cache["lolalytics"] = {"mid": {"MALZAHAR": {"wr": 50.0, "vs": {
            # 목록 전체 평균 대비로 중심화되므로 비교 대상 행을 함께 둔다
            "top": [[4, 40.0, 1000, 50.0], [8, 52.0, 3000, 50.0]],
            "mid": [[2, 55.0, 2000, 49.0], [3, 47.0, 2000, 49.0]],
        }}}}
        cache["lolalytics_meta"] = {"tier": "emerald_plus", "patch": "30"}
        self.p = Picker(cache)

    def test_cross_lane_enemy_lowers_score(self):
        base, _, _ = self.p.score("mid", "MALZAHAR", {}, {})
        vs_singed, factors, _ = self.p.score("mid", "MALZAHAR", {"top": "SINGED"}, {})
        self.assertLess(vs_singed, base - 0.03)
        self.assertTrue(any(f[0] == "상대 탑 신지드" for f in factors))

    def test_lane_opponent_prefers_lolalytics(self):
        _, factors, _ = self.p.score("mid", "MALZAHAR", {"mid": "TALON"}, {})
        lane = [f for f in factors if f[0].startswith("맞라인")]
        self.assertEqual(len(lane), 1)              # OP.GG 값과 중복 반영하지 않음
        self.assertIn("LoLalytics", lane[0][3])
        self.assertGreater(lane[0][1], 0)           # 55% 전적 → 유리

    def test_centering_removes_global_bias(self):
        # 모든 상대에게 똑같이 +5%p 높게 나오는 목록이면 상호작용은 0이어야 한다
        p = self.p
        p.lola["mid"]["MALZAHAR"]["vs"]["adc"] = [[6, 55.0, 1000, 50.0]]
        _, factors, _ = p.score("mid", "MALZAHAR", {"adc": "KAISA"}, {})
        f = [x for x in factors if x[0].startswith("상대 원딜")][0]
        self.assertAlmostEqual(f[1], 0.0, places=6)

    def test_missing_enemy_is_reported(self):
        _, _, notes = self.p.score("mid", "MALZAHAR", {"jungle": "WUKONG"}, {})
        self.assertTrue(any("오공" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
