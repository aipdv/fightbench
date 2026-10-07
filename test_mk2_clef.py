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

# read_flags: the enemy attack flag is the byte at 0xFFB6FB in byte-swapped Genesis RAM.
class RamEnv:
    def __init__(self, enemy_flag):
        self.unwrapped, self.ram = self, bytearray(0x10000)
        self.ram[0xB6FB ^ 1] = enemy_flag
    def get_ram(self):
        return self.ram

for flag, attacking in ((240, True), (252, True), (0, False), (241, False)):
    info = {}
    mk2_clef.read_flags(RamEnv(flag), info)
    assert info == {"enemy_attacking": attacking, "me_attacking": False}, (flag, info)

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
full_log = [{"frame": d["frame"], "situation": d["situation"], "action": d["action"], "latency_ms": 100,
             "dropped": False, "apply_frame": d["frame"] + 6, "done_frame": d["frame"] + 6} for d in a["decisions"]]
live_log = full_log[:-1]  # the last decision is at frame 900, after the fake round ends


def live_sub(decisions, fps=60):
    return {"interval_s": 0.12, "max_inflight": 3,
            "rounds": [{"fight": fb.LADDER[0], "repeat": 0, "fps": fps, "decisions": decisions}]}


def rejects(fn, *args):
    try:
        fn(*args)
    except ValueError:
        return True
    return False


fb.check_live(live_sub(live_log))
c, d = (fb.replay_live_round(FakeEnv(), fb.LADDER[0], live_log) for _ in range(2))
c.pop("fps"), d.pop("fps")
assert c == d and c["damage_dealt"] > 0, c
assert rejects(fb.replay_live_round, FakeEnv(), fb.LADDER[0], full_log)  # a request the replayed round never reached

# Live logs the runner cannot write are rejected: odd frames, zero latency, out-of-order applies, and bad drops.
d0, d1 = live_log[:2]  # requests at frames 15 and 30, answers applied at 21 and 36
late_drop = live_log[:]
late_drop[0] = d0 | {"action": None, "dropped": True, "apply_frame": None, "done_frame": 40}
fb.check_live(live_sub(late_drop))  # it arrived after the newer answer was applied at 36
for bad in ([d0 | {"frame": 15.0}] + live_log[1:],
            [d0 | {"action": None, "apply_frame": None, "done_frame": None}] + live_log[1:],
            [d0 | {"apply_frame": 15, "done_frame": 15}] + live_log[1:],
            [d0 | {"apply_frame": 40, "done_frame": 40}] + live_log[1:],
            [late_drop[0] | {"done_frame": 22}] + live_log[1:]):
    assert rejects(fb.check_live, live_sub(bad)), bad[0]

# play_live with a 2 s stall on a fake clock. The runner must still write a log that check_live and replay accept.
from concurrent.futures import Future


class Clock:
    t = 0.0
    def perf_counter(self):
        return self.t
    def sleep(self, s):
        self.t += s


class SyncPool:  # every answer arrives on the next frame, so the log is the same on every run
    def __init__(self, workers):
        pass
    def submit(self, fn, *args):
        f = Future()
        f.set_result(fn(*args))
        return f
    def shutdown(self, **kw):
        pass


class Uniform(fb.Policy):
    def distributions(self, fight_id, state_line, situations, moves):
        return {s: uniform for s in situations}


def stalling_press(env, buttons):
    clock.t += 0.2 if 100 <= env.f < 110 else 0
    return fake_press(env, buttons)


clock, real = Clock(), (fb.time, fb.ThreadPoolExecutor)
fb.time, fb.ThreadPoolExecutor, mk2_clef.press = clock, SyncPool, stalling_press
played = fb.play_live(FakeEnv(), Uniform(), fb.LADDER[0], fb.seeded(0, 0))
(fb.time, fb.ThreadPoolExecutor), mk2_clef.press = real, fake_press
fb.check_live(live_sub(played["decisions"], played["fps"]))
assert fb.replay_live_round(FakeEnv(), fb.LADDER[0], played["decisions"])["damage_dealt"] == played["damage_dealt"]

# Row ids and submission names carry the model version. Floor rows ('harness') do not.
clef = {"model": "clef-flash", "model_version": "@cf/cloudflare/clef-flash", "track": "policy_table"}
assert fb.row_id(clef, False) == "clef-flash--cf-cloudflare-clef-flash__policy_table__mk2-liukang-v1"
assert fb.row_id(clef | {"model_version": "v2"}, False) != fb.row_id(clef, False)
assert fb.row_id({"model": "script", "model_version": "harness", "track": "policy_table"}, True) == (
    "script__policy_table__mk2-liukang-v1__smoke")
for path in fb.SUBMISSIONS.glob("*.json"):  # each committed submission has the name its replayed row gets
    sub = json.loads(path.read_text())
    official = fb.check_tables(sub["tables"]) if sub["track"] == "policy_table" else fb.check_live(sub)
    assert path.stem == fb.row_id(sub, not official), path.name

# The board puts official rows above smoke rows, then sorts by mean damage dealt.
import tempfile
from pathlib import Path

real = fb.VERIFIED, fb.BOARD
with tempfile.TemporaryDirectory() as tmp:
    fb.VERIFIED, fb.BOARD = Path(tmp), Path(tmp) / "board.json"
    base = {"track": "policy_table", "model_version": "v", "won": False, "damage_taken": 0, "decisions": []}
    for model, official, dealt in (("lucky", False, 90), ("full", True, 10), ("better", True, 30)):
        row = base | {"model": model, "official": official, "damage_dealt": dealt}
        (fb.VERIFIED / f"{model}.jsonl").write_text(json.dumps(row) + "\n")
    fb.write_board()
    assert [r["model"] for r in json.loads(fb.BOARD.read_text())["policy_table"]] == ["better", "full", "lucky"]

# replay(): the same replay twice is accepted; a replay that differs exits and keeps the old file; bad files are rejected.
import contextlib
import io

with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
    fb.HERE, fb.VERIFIED, fb.BOARD = Path(tmp), Path(tmp), Path(tmp) / "board.json"
    fb.replay(FakeEnv(), "script", smoke=True)
    fb.replay(FakeEnv(), "script", smoke=True)
    out = fb.VERIFIED / "script__policy_table__mk2-liukang-v1__smoke.jsonl"
    out.write_text("old\n")
    try:
        fb.replay(FakeEnv(), "script", smoke=True)
        raise AssertionError("a differing replay overwrote the old file")
    except SystemExit:
        assert out.read_text() == "old\n"
    sub = {"edition": fb.EDITION, "temperature": 1, "rom_sha1": fb.ED["rom_sha1"], "track": "policy_table",
           "model": "m", "model_version": "v", "tables": smoke_table}
    half = {m: 0.5 / 17 for m in fb.MOVE_IDS}  # sums to 0.5: rejected, never renormalized
    for bad in (sub | {"temperature": 0.5}, sub | {"tables": {fb.LADDER[0]: {s: half for s in fb.SITUATIONS}}}):
        path = Path(tmp) / "sub.json"
        path.write_text(json.dumps(bad))
        try:
            fb.replay(FakeEnv(), str(path))
            raise AssertionError(f"accepted {bad}")
        except SystemExit:
            pass
    assert not list(fb.VERIFIED.glob("m*.jsonl"))
fb.HERE, (fb.VERIFIED, fb.BOARD) = Path(fb.__file__).parent, real

print("ok")
