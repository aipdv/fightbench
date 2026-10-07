"""Checks for the pure logic. Run: python3 test_mk2_clef.py"""
import random

from mk2_clef import ACTIONS, VARIANTS, build_state, parse_arm, plan_for, questions, reflex_block, sample, script_policy

# Every action maps to buttons. Buttons follow the 3-button layout checked with --calibrate.
for a in ACTIONS:
    assert plan_for(a, "RIGHT", "LEFT"), a
assert plan_for("approach", "RIGHT", "LEFT")[0] == {"RIGHT"}
assert plan_for("retreat", "LEFT", "RIGHT")[0] == {"RIGHT"}
assert plan_for("high_punch", "RIGHT", "LEFT")[0] == {"RIGHT", "A"}  # toward + A
assert plan_for("low_punch", "RIGHT", "LEFT")[0] == {"A"}
assert plan_for("sweep", "RIGHT", "LEFT")[0] == {"LEFT", "B"}  # away + low kick
assert plan_for("crouch_block", "RIGHT", "LEFT")[0] == {"DOWN", "START"}
up = plan_for("uppercut", "LEFT", "RIGHT")
assert up[0] == {"DOWN"} and up[8] == {"DOWN", "LEFT", "A"}, up  # crouch first, then down + high punch

# Temperature 0 takes the top choice. Low temperature mostly does too.
probs = {"block": 0.2, "high_punch": 0.7, "sweep": 0.1}
assert sample(probs, 0) == "high_punch"
rng = random.Random(1)
assert sum(sample(probs, 0.3, rng) == "high_punch" for _ in range(1000)) > 900

# Memory -> state. Enemy on the right walking left = coming toward me.
# y_position is vertical speed: the enemy is airborne if it was non-zero in the last 5 frames (covers the jump apex).
def frame(ex, ey):
    return {"x_position": 100, "enemy_x_position": ex, "y_position": 0, "enemy_y_position": ey,
            "health": 60, "enemy_health": 120, "rounds_won": 0, "enemy_rounds_won": 1}
history = [frame(200, 0)] * 10 + [frame(195, -2), frame(192, 0), frame(191, 0), frame(190, 0), frame(190, 0)]
s = build_state(history, "Level1.LiuKangVsJax")
assert s["enemy_side"] == "right" and s["enemy"]["moving"] == "toward me", s
assert s["enemy"]["airborne"] and not s["me"]["airborne"], s
assert not build_state([frame(190, 0)] * 15, "x")["enemy"]["airborne"]
assert s["me"]["health_pct"] == 50 and s["health_lead_pct"] == -50, s
assert s["distance"] == "mid" and s["matchup"].startswith("You are LiuKang"), s
assert s["attacks_in_reach"] == ["low_kick", "sweep"], s  # 90 px: only the long low attacks reach
assert build_state([frame(250, 0)] * 15, "x")["attacks_in_reach"] == []
from mk2_clef import matchup
assert matchup("VeryEasy.LiuKang-08") == "You are LiuKang (player 1). The CPU is Scorpion.", matchup("VeryEasy.LiuKang-08")

# Tactics variant: specials are forward, forward + button; hit signals come from the 0.75 s history.
fb = plan_for("fireball_high", "RIGHT", "LEFT")
assert fb[:3] == [{"RIGHT"}] * 3 and fb[3:6] == [set()] * 3 and fb[6] == {"RIGHT", "A"}, fb
assert plan_for("flying_kick", "LEFT", "RIGHT")[6] == {"LEFT", "C"}
assert plan_for("fireball_low", "RIGHT", "LEFT")[11] == {"A"}
hit = [frame(190, 0)] * 30 + [dict(frame(190, 0), health=50)] * 15
t = build_state(hit, "VeryEasy.LiuKang-02", "tactics")
assert t["i_got_hit_recently"] and not t["i_hit_enemy_recently"], t
assert "fireball_high" in t["attacks_in_reach"] and "i_got_hit_recently" not in build_state(hit, "x"), t
assert set(VARIANTS["baseline"]["actions"]) < set(questions("tactics")["action"]["criteria"])

# v2 variant: state is a string; every action description has "Use when"; questions has threat.
def frame_ea(ex, ey, ea=False):
    f = frame(ex, ey)
    f["enemy_attacking"] = ea
    return f

hist_v2_atk = [frame_ea(200, 0)] * 10 + [frame_ea(190, 0, ea=True)] * 3
s_v2 = build_state(hist_v2_atk, "VeryEasy.LiuKang-08", "v2")
assert isinstance(s_v2, str), "v2 state must be a string"
assert "Enemy attacking now: yes" in s_v2, s_v2

for k, v in VARIANTS["v2"]["actions"].items():
    assert "Use when" in v, f"v2 action {k!r} missing 'Use when': {v!r}"

q_v2 = questions("v2")
assert "action" in q_v2 and "threat" in q_v2, q_v2
assert "threat" not in questions("tactics"), "threat must not appear in non-v2 questions"

# reflex_block: pure function; True = attacking + close + grounded, False otherwise.
def rframe(ea=False, dist=50, y=0):
    return {"x_position": 0, "enemy_x_position": dist, "y_position": y, "enemy_y_position": 0, "enemy_attacking": ea}

assert reflex_block([rframe(ea=True, dist=50)] * 5), "should block: attacking, close, grounded"
assert not reflex_block([rframe(ea=True, dist=150)] * 5), "should not block: far"
assert not reflex_block([rframe(ea=False, dist=50)] * 5), "should not block: not attacking"
assert not reflex_block([rframe(ea=True, dist=50, y=1)] * 5), "should not block: airborne"

# script_policy (pure, no emulator): close airborne → uppercut, attacking close → crouch_block,
# far idle → fireball/flying_kick; arm-name parsing → policy, reflex, lag.
def sframe(ex, ey=0, ea=False):
    return {"x_position": 100, "enemy_x_position": ex, "y_position": 0, "enemy_y_position": ey,
            "health": 60, "enemy_health": 120, "enemy_attacking": ea}

_rng = random.Random(0)
close_air = [sframe(160, 0)] * 10 + [sframe(160, -2)]  # dx=60, airborne
assert script_policy(close_air, _rng) == "uppercut", "close airborne → uppercut"
atk_close = [sframe(140, ea=True)]  # dx=40 ≤ 100, attacking, grounded
assert script_policy(atk_close, _rng) == "crouch_block", "attacking close → crouch_block"
far_idle = [sframe(320)] * 15  # dx=220 > 100, grounded, idle
assert script_policy(far_idle, _rng) in ("fireball_high", "fireball_low"), "far idle → fireball"
spec, policy_fn, use_reflex, lag = parse_arm("script+reflex@12")
assert policy_fn is script_policy, "parse_arm: policy"
assert use_reflex is True, "parse_arm: reflex"
assert lag == 12, "parse_arm: lag"


# Situation detection for the Clef arm: jump beats everything near, then attack, then distance.
from mk2_clef import situation
assert situation([frame(190, 0)] * 4 + [frame(190, -3)]) == "enemy_jump"
assert situation([frame(150, 0)] * 5) == "point_blank"
assert situation([frame(175, 0)] * 5) == "close"
assert situation([frame(190, 0)] * 5) == "mid"
assert situation([frame(240, 0)] * 5) == "far"
assert situation([frame(300, 0)] * 5) == "full_screen"
assert situation([frame(190, 0)] * 4 + [frame(190, 0) | {"enemy_attacking": True}]) == "close_attack"

# FightBench edition 1: the frozen text, the detector edges, the random control, rejection, and replay determinism.
import json
import math
import fightbench as fb
import mk2_clef
from mk2_clef import TABLE_PROMPTS

EDITION_SITUATIONS = {
    "enemy_jump": "The enemy is in the air, less than 150 px away.",
    "close_attack": "The enemy is attacking, less than 100 px away.",
    "far_attack": "The enemy is attacking from more than 100 px away, maybe with a projectile.",
    "point_blank": "The enemy is less than 60 px away and not attacking.",
    "close": "The enemy is 60 to 80 px away and not attacking.",
    "mid": "The enemy is 80 to 100 px away and not attacking.",
    "far": "The enemy is 100 to 150 px away and not attacking.",
    "full_screen": "The enemy is more than 150 px away and not attacking.",
}
EDITION_MOVES = [
    ("approach", "Walk toward the enemy."),
    ("retreat", "Walk away from the enemy."),
    ("jump_in", "Jump toward the enemy. Easy to punish if the enemy is ready."),
    ("jump_back", "Jump away from the enemy."),
    ("block", "Stand and block. Stops high and mid attacks."),
    ("crouch_block", "Crouch and block. Stops low attacks."),
    ("duck", "Crouch without blocking. High projectiles fly over you."),
    ("high_punch", "Fast high punch. Reach 70 px. Under 50 px it becomes a throw, which beats blocking."),
    ("low_punch", "Fast low punch. Reach 70 px."),
    ("high_kick", "High kick. Reach 79 px."),
    ("low_kick", "Low kick. Reach 90 px."),
    ("uppercut", "Crouching uppercut. Reach 70 px. Big damage, beats a jump-in."),
    ("sweep", "Back + low kick. Reach 94 px. Knocks the enemy down."),
    ("roundhouse", "Back + high kick. Reach 75 px. Knocks the enemy away."),
    ("fireball_high", "High fireball. Hits at any distance. 17 damage."),
    ("fireball_low", "Low fireball. Hits at any distance. 17 damage."),
    ("flying_kick", "Flying kick. Crosses 100 to 150 px fast. 20 damage."),
]
assert fb.SITUATIONS == EDITION_SITUATIONS == TABLE_PROMPTS["clef2"][0]
assert list(fb.MOVES.items()) == EDITION_MOVES == list(TABLE_PROMPTS["clef2"][1].items())
assert fb.state_line("LiuKangVsBaraka_VeryHard_01") == "You are LiuKang. The CPU is Baraka."
assert len(fb.LADDER) == 15 and fb.LADDER[-1] == "LiuKangVsShaoKahn_VeryHard_15"

# situation() edges. frame(ex, ey) puts the player at x = 100, so dx = ex - 100.
def dx(d, ey=0, atk=False):
    return [frame(100 + d, 0)] * 4 + [frame(100 + d, ey) | {"enemy_attacking": atk}]
assert [situation(dx(d)) for d in (59, 60, 79, 80, 100, 101, 150, 151)] == [
    "point_blank", "close", "close", "mid", "mid", "far", "far", "full_screen"]
assert situation(dx(149, ey=-1)) == "enemy_jump" and situation(dx(150, ey=-1)) == "far"
assert situation(dx(100, atk=True)) == "close_attack" and situation(dx(101, atk=True)) == "far_attack"
assert mk2_clef._ENEMY_ATK_ADDR == 16758523 and mk2_clef._ATK_FLAGS == {240, 244, 246, 250, 252}

# Official random picks from the 17 edition moves, not the old 13-move ACTIONS.
_rng = fb.seeded(0, 0)
assert {fb.random_policy([], _rng) for _ in range(2000)} == set(fb.MOVE_IDS)

# A table that sums wrong, misses a move, or has a negative value is rejected. Nothing is renormalized.
uniform = {m: 1 / 17 for m in fb.MOVE_IDS}
fb.check_dist(uniform)
for bad in ({**uniform, "block": 0.5}, {k: v for k, v in uniform.items() if k != "duck"},
            {**uniform, "block": -1 / 17, "duck": 3 / 17}, {**uniform, "block": math.nan}):
    try:
        fb.check_dist(bad)
        raise AssertionError(f"accepted {bad}")
    except ValueError:
        pass
smoke_table = {fb.LADDER[0]: {s: uniform for s in fb.SITUATIONS}}
assert fb.check_tables(smoke_table) is False
try:
    fb.check_tables({fb.LADDER[0]: {"close": uniform}})  # policy-table files need all 8 situations
    raise AssertionError("accepted a table with 1 situation")
except ValueError:
    pass

# Replay with a fake emulator: the same table or action log gives the same result twice. No ROM needed.
class FakeEnv:
    def load_state(self, name, inttype=None):
        self.f, self.me, self.enemy, self.ex = 0, 120, 120, 300
    def reset(self):
        pass

def fake_press(env, buttons):
    env.f += 1
    env.ex = max(130, env.ex - 2)
    env.enemy -= ("A" in buttons or "B" in buttons) and env.f % 7 == 0
    env.me -= env.f % 23 == 0
    info = {"x_position": 100, "enemy_x_position": env.ex, "y_position": 0, "enemy_y_position": 0,
            "health": env.me, "enemy_health": env.enemy}
    return None, 0, env.f >= 900, False, info

mk2_clef.press, mk2_clef.read_flags = fake_press, lambda env, info: info.update(enemy_attacking=env.f % 40 < 3)
skewed = {m: (0.5 if m == "low_kick" else 0.5 / 16) for m in fb.MOVE_IDS}
table = fb.table_policy({fb.LADDER[0]: {s: skewed for s in fb.SITUATIONS}})
a, b = (fb.table_round(FakeEnv(), fb.LADDER[0], table, fb.seeded(0, 0)) for _ in range(2))
assert a == b and a["damage_dealt"] > 0 and len(a["decisions"]) == 60, a["damage_dealt"]
live_log = [{"frame": d["frame"], "situation": d["situation"], "action": d["action"], "latency_ms": 100,
             "dropped": False, "apply_frame": d["frame"] + 6, "done_frame": d["frame"] + 6} for d in a["decisions"]]
fb.check_live({"interval_s": 0.12, "max_inflight": 3,
               "rounds": [{"fight": fb.LADDER[0], "repeat": 0, "fps": 60, "decisions": live_log}]})
c, d = (fb.replay_live_round(FakeEnv(), fb.LADDER[0], live_log) for _ in range(2))
c.pop("fps"), d.pop("fps")
assert c == d and c["damage_dealt"] > 0, c

print("ok")
