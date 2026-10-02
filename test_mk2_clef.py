"""Checks for the pure logic. Run: python3 test_mk2_clef.py"""
import random

from mk2_clef import ACTIONS, build_state, plan_for, sample

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
assert matchup("VeryEasy.LiuKang-02").startswith("You are LiuKang"), matchup("VeryEasy.LiuKang-02")
print("ok")
