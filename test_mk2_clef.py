"""Checks for the pure logic. Run: python3 test_mk2_clef.py"""
import random

from mk2_clef import ACTIONS, VARIANTS, build_state, plan_for, questions, reflex_block, sample

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

print("ok")
