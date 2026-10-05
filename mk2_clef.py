# /// script
# requires-python = ">=3.10"
# dependencies = ["stable-retro==1.0.1", "pillow"]
# ///
"""Clef-flash plays Mortal Kombat II (Genesis) from game memory.

Run: uv run mk2_clef.py   then open http://localhost:8000 and press Play.
"""
import argparse
import base64
import http.client
import io
import json
import os
import random
import re
import statistics
import threading
import time
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
GAME = "MortalKombatII-Genesis-v0"
FPS = 60
FREE_NEURONS_PER_DAY = 10_000
NEURONS_PER_M_INPUT = {"clef-flash": 8182, "clef": 21818}  # Workers AI pricing page, 2026-10-01
USD_PER_1K_NEURONS = 0.011

INTRO_FRAMES = 200  # "ROUND 1" and "FIGHT!" in Level1 states (calibration waits this out)
MAX_HEALTH = 120  # full health bar in memory
DECISION_EVERY = 15  # frames between policy decisions in sim mode (0.25 s)

# stable-retro emulates a 3-button pad: X, Y and Z do nothing. High punch is toward + A.
# Checked frame by frame with --calibrate on the (W) ROM, 2026-10-02.
PAD = {"LP": {"A"}, "HP": {"TOWARD", "A"}, "LK": {"B"}, "HK": {"C"}, "BL": {"START"}}

ACTIONS = {
    "approach": "Walk toward the enemy.",
    "retreat": "Walk away from the enemy.",
    "jump_in": "Jump toward the enemy.",
    "jump_back": "Jump away from the enemy.",
    "block": "Stand and block high attacks.",
    "crouch_block": "Crouch and block low attacks.",
    "high_punch": "Fast high punch. Close range.",
    "low_punch": "Fast low punch. Close range.",
    "high_kick": "High kick. A little more reach than a punch.",
    "low_kick": "Low kick. A little more reach than a punch.",
    "uppercut": "Crouching uppercut. Big damage at very close range, and beats a jump-in.",
    "sweep": "Back plus low kick. Trips the enemy at close range.",
    "roundhouse": "Back plus high kick. Knocks the enemy away at close range.",
}
# Longest distance (px) at which each attack still hit an idle Jax as Liu Kang (2P state, 2026-10-02).
# ponytail: one character pair. Reach differs a little per fighter.
REACH = {"low_punch": 70, "high_punch": 70, "uppercut": 70, "roundhouse": 75, "high_kick": 79, "low_kick": 90, "sweep": 94}

VARIANTS = {
    "baseline": {
        "actions": ACTIONS,
        "reach": REACH,
        "instructions": "You are player 1 in Mortal Kombat II. Pick the next move that helps you win the round. "
        "Attack only with a move in attacks_in_reach. If none reach, approach, jump in, or block.",
    },
    # A/B candidate from a review of the demo video (2026-10-02). Specials are Liu Kang only.
    "tactics": {
        "actions": {
            **ACTIONS,
            "high_punch": "High punch. At point-blank range it becomes a throw, which beats blocking.",
            "uppercut": "Crouching uppercut. Best against an enemy jumping at you. Risky if blocked up close.",
            "duck": "Crouch without blocking. High projectiles fly over you.",
            "fireball_high": "Liu Kang high fireball. Hits from any distance.",
            "fireball_low": "Liu Kang low fireball. Hits from any distance.",
            "flying_kick": "Liu Kang flying kick. Crosses mid and long range fast.",
        },
        # Fireballs and flying kick hit an idle Jax at 100 and 148 px (2P state, 2026-10-02).
        "reach": {**REACH, "flying_kick": 150, "fireball_high": 999, "fireball_low": 999},
        "instructions": "You are player 1 in Mortal Kombat II. Pick the next move that helps you win the round. "
        "Attack only with a move in attacks_in_reach. Do not jump in from far away: the CPU punishes jumps. "
        "At long range, throw a fireball or duck. Block right after you get hit.",
    },
    # Control arm: uniform random baseline moves, no Clef call, same delay as a typical Clef answer.
    "random": {"actions": ACTIONS, "reach": REACH, "instructions": ""},
    # Control arm: the hand-written script_policy at Clef's pace, no API calls.
    "script": {"actions": {}, "reach": REACH, "instructions": ""},
}

# v2: same 17 moves as tactics, descriptions rewritten with "Use when" clauses for Clef-flash research finding.
VARIANTS["v2"] = {
    "actions": {
        "approach": "Walk toward enemy. Use when no attack reaches and Enemy attacking now: no.",
        "retreat": "Walk away from enemy. Use when Your health: critical and Enemy attacking now: yes.",
        "jump_in": "Jump toward enemy. Use when distance is far and Enemy attacking now: no and Enemy airborne: no.",
        "jump_back": "Jump away from enemy. Use when You got hit recently: yes and distance is very close.",
        "block": "Stand and block. Use when Enemy attacking now: yes and distance is close or very close.",
        "crouch_block": "Crouch and block low attacks. Use when Enemy attacking now: yes.",
        "high_punch": "Fast high punch; at point-blank it becomes a throw beating a block. Use when distance is very close and Enemy attacking now: no.",
        "low_punch": "Fast low punch. Use when distance is very close and Enemy attacking now: no.",
        "high_kick": "High kick, more reach than a punch. Use when distance is close and Enemy attacking now: no.",
        "low_kick": "Low kick, longest basic reach. Use when distance is close or mid and Enemy attacking now: no.",
        "uppercut": "Crouching uppercut, beats jump-ins. Use when Enemy airborne: yes and distance is close or very close.",
        "sweep": "Back plus low kick, trips enemy. Use when distance is close and Enemy attacking now: no.",
        "roundhouse": "Back plus high kick, knocks enemy away. Use when distance is very close and Enemy attacking now: no.",
        "duck": "Crouch without blocking; high projectiles fly over. Use when distance is far and Enemy attacking now: yes.",
        "fireball_high": "Liu Kang high fireball, hits from any distance. Use when distance is far and Enemy attacking now: no.",
        "fireball_low": "Liu Kang low fireball, hits from any distance. Use when distance is far and Enemy airborne: no and Enemy attacking now: no.",
        "flying_kick": "Liu Kang flying kick, crosses range fast. Use when distance is mid or far and Enemy attacking now: no.",
    },
    "reach": VARIANTS["tactics"]["reach"],
    "instructions": "What should Liu Kang do next?",
}

# RAM addresses for attack-flag signals (abs 68000 addresses, decimal). idx = (addr - 0xFF0000) ^ 1.
_ENEMY_ATK_ADDR = 16758523
_ME_ATK_ADDR = 16758283
_ATK_FLAGS = {240, 244, 246, 250, 252}


def read_flags(env, info):
    """Read atk_flags from emulator RAM and annotate info in-place. Call after every env.step."""
    ram = env.unwrapped.get_ram()
    def _flag(addr):
        return ram[(addr - 0xFF0000) ^ 1]
    info["enemy_attacking"] = _flag(_ENEMY_ATK_ADDR) in _ATK_FLAGS
    info["me_attacking"] = _flag(_ME_ATK_ADDR) in _ATK_FLAGS


def reflex_block(history):
    """Pure: should we block this frame? True when enemy was attacking last frame, close, and player grounded."""
    if not history:
        return False
    if not history[-1].get("enemy_attacking"):
        return False
    dx = abs(history[-1].get("enemy_x_position", 999) - history[-1].get("x_position", 0))
    if dx > 100:
        return False
    return not any(h.get("y_position", 0) != 0 for h in history[-5:])


# Clef table prompts: situations the executor can detect, and the moves Clef picks from.
TABLE_PROMPTS = {
    "clef": ({
        "enemy_jump": "The enemy jumps at you.",
        "close_attack": "The enemy attacks you up close.",
        "far_attack": "The enemy attacks from far away, maybe with a projectile.",
        "point_blank": "You stand face to face. The enemy is not attacking.",
        "close": "The enemy is at kicking distance and not attacking.",
        "far": "The enemy is far away and not attacking.",
    }, VARIANTS["tactics"]["actions"]),
    # v2: measured reach in every move, distance bands in every situation.
    "clef2": ({
        "enemy_jump": "The enemy is in the air, less than 150 px away.",
        "close_attack": "The enemy is attacking, less than 100 px away.",
        "far_attack": "The enemy is attacking from more than 100 px away, maybe with a projectile.",
        "point_blank": "The enemy is less than 60 px away and not attacking.",
        "close": "The enemy is 60 to 80 px away and not attacking.",
        "mid": "The enemy is 80 to 100 px away and not attacking.",
        "far": "The enemy is 100 to 150 px away and not attacking.",
        "full_screen": "The enemy is more than 150 px away and not attacking.",
    }, {
        "approach": "Walk toward the enemy.",
        "retreat": "Walk away from the enemy.",
        "jump_in": "Jump toward the enemy. Easy to punish if the enemy is ready.",
        "jump_back": "Jump away from the enemy.",
        "block": "Stand and block. Stops high and mid attacks.",
        "crouch_block": "Crouch and block. Stops low attacks.",
        "duck": "Crouch without blocking. High projectiles fly over you.",
        "high_punch": "Fast high punch. Reach 70 px. Under 50 px it becomes a throw, which beats blocking.",
        "low_punch": "Fast low punch. Reach 70 px.",
        "high_kick": "High kick. Reach 79 px.",
        "low_kick": "Low kick. Reach 90 px.",
        "uppercut": "Crouching uppercut. Reach 70 px. Big damage, beats a jump-in.",
        "sweep": "Back + low kick. Reach 94 px. Knocks the enemy down.",
        "roundhouse": "Back + high kick. Reach 75 px. Knocks the enemy away.",
        "fireball_high": "High fireball. Hits at any distance. 17 damage.",
        "fireball_low": "Low fireball. Hits at any distance. 17 damage.",
        "flying_kick": "Flying kick. Crosses 100 to 150 px fast. 20 damage.",
    }),
}
# v3: coach edits from measured outcomes of v2 (ab/sim logs, 2026-10-05): attacks up close were 17% of the time
# and 62% of the damage taken; trading lost, block lost least; fireballs were punished inside 100 px.
_V3_SIT, _V3_MOVES = dict(TABLE_PROMPTS["clef2"][0]), dict(TABLE_PROMPTS["clef2"][1])
_V3_SIT["close_attack"] = ("The enemy's attack is already coming out, less than 100 px away. "
                           "An attack you start now usually trades or loses.")
_V3_MOVES.update({
    "block": "Stand and block. Stops high and mid attacks. The safest answer when an attack is already coming.",
    "fireball_high": "High fireball. Hits at any distance. 17 damage. Slow to start: best from more than 100 px.",
    "fireball_low": "Low fireball. Hits at any distance. 17 damage. Slow to start: best from more than 100 px.",
    "flying_kick": "Flying kick. Crosses 100 to 150 px fast. 20 damage. Misses an enemy in the air.",
})
TABLE_PROMPTS["clef3"] = (_V3_SIT, _V3_MOVES)
# v4: Clef matches words, so each move gets a "Use when" that repeats the situation text where v2's measured
# outcomes were best (more dealt, less taken). v3's conditional clauses backfired: "best from more than 100 px"
# raised fireball picks inside 100 px.
_S = TABLE_PROMPTS["clef2"][0]
_V4_USE = {
    "flying_kick": ["far", "full_screen"], "approach": ["full_screen"], "sweep": ["mid"], "low_kick": ["close", "mid"],
    "block": ["close_attack"], "roundhouse": ["close_attack"], "high_punch": ["point_blank"],
    "fireball_high": ["far_attack"], "uppercut": ["enemy_jump"],
}
TABLE_PROMPTS["clef4"] = (_S, {m: d + (" Use when: " + " Or: ".join(_S[k] for k in _V4_USE[m]) if m in _V4_USE else "")
                               for m, d in TABLE_PROMPTS["clef2"][1].items()})
COARSER = {"mid": "close", "full_screen": "far"}  # for prompts without the finer bands


def situation(history):
    """Which situation holds now. The only game logic in the Clef arm: detection, no choices."""
    info = history[-1]
    dx = abs(info["enemy_x_position"] - info["x_position"])
    if dx < 150 and any(h["enemy_y_position"] != 0 for h in history[-5:]):
        return "enemy_jump"
    attacking = any(h.get("enemy_attacking") for h in history[-3:])
    if attacking:
        return "far_attack" if dx > 100 else "close_attack"
    return "point_blank" if dx < 60 else "close" if dx < 80 else "mid" if dx <= 100 else "far" if dx <= 150 else "full_screen"


class ClefTable:
    """Clef answers one question per situation; code plays Clef's pick for the situation it sees.
    Answers are cached on disk by request, so repeats are free and there is no delay."""

    def __init__(self, clef, temperature, prompt="clef", path=HERE / "ab" / "clef-cache.json"):
        self.clef, self.temperature, self.path, self.tokens = clef, temperature, path, 0
        self.situations, self.moves = TABLE_PROMPTS[prompt]
        self.cache = json.loads(path.read_text()) if path.exists() else {}

    def tables(self, state_name):
        body = {"model": self.clef.model, "state": matchup(state_name).replace(" (player 1)", ""),
                "questions": {k: {"type": "choice", "instructions": f"{d} Which move do you use?",
                                  "criteria": self.moves} for k, d in self.situations.items()}}
        key = json.dumps(body, sort_keys=True)
        if key not in self.cache:
            r = self.clef.ask(body)
            self.tokens += r.get("usage", {}).get("input_tokens", 0)
            self.cache[key] = {k: a["probabilities"] for k, a in r["answers"].items()}
            self.path.write_text(json.dumps(self.cache))
        return self.cache[key]

    def __call__(self, history, rng, state_name=None):
        sit = situation(history)
        return sample(self.tables(state_name)[sit if sit in self.situations else COARSER[sit]], self.temperature, rng)


def _random_policy(history, rng, state_name=None):
    return rng.choice(list(ACTIONS))


def script_policy(history, rng, state_name=None):
    """Pure rules policy for sim experiments. < 25 lines. Uses signals available in RAM memory."""
    if not history:
        return rng.choice(list(ACTIONS))
    info = history[-1]
    dx = abs(info.get("enemy_x_position", 200) - info.get("x_position", 0))
    airborne = any(h.get("enemy_y_position", 0) != 0 for h in history[-5:])
    enemy_atk = any(h.get("enemy_attacking", False) for h in history[-3:])
    if airborne and dx <= 80:
        return "uppercut"
    if enemy_atk and dx <= 100:
        return "crouch_block"
    if dx > 100:
        choices = ["fireball_high", "fireball_low", "flying_kick"] if dx <= 150 else ["fireball_high", "fireball_low"]
        return rng.choice(choices)
    if dx <= 80:
        return rng.choice(["high_punch", "roundhouse", "sweep", "uppercut", "low_kick"])
    return rng.choice(["sweep", "low_kick"])  # 80 < dx <= 100, not attacking


def parse_arm(spec, tables=None):
    """'script+reflex@12' → (spec_str, policy_fn, use_reflex, lag). Default lag=24."""
    base, lag = spec, 24
    if "@" in spec:
        base, lag_s = spec.rsplit("@", 1)
        lag = int(lag_s)
    use_reflex = base.endswith("+reflex")
    policy_name = base.removesuffix("+reflex")
    policy = {"script": script_policy, **(tables or {})}.get(policy_name, _random_policy)
    return spec, policy, use_reflex, lag


def questions(variant):
    v = VARIANTS[variant]
    q = {"action": {"type": "choice", "instructions": v["instructions"], "criteria": v["actions"]}}
    if variant == "v2":
        q["threat"] = {"type": "noul", "instructions": "Will the enemy hit you within the next half second?"}
    return q


def plan_for(action, toward, away):
    """Per-frame button sets for one action. toward and away are 'LEFT' or 'RIGHT'."""

    def pad(*keys):  # move names (PAD keys), TOWARD/AWAY, or raw buttons -> raw buttons
        out = set().union(*(PAD.get(k, {k}) for k in keys))
        return {toward if b == "TOWARD" else away if b == "AWAY" else b for b in out}

    def hold(*keys):
        return [(pad(*keys), 15)]

    def tap(*keys):
        return [(pad(*keys), 4), (set(), 2)]

    plan = {
        "approach": hold("TOWARD"),
        "retreat": hold("AWAY"),
        "jump_in": [(pad("UP", "TOWARD"), 6)],
        "jump_back": [(pad("UP", "AWAY"), 6)],
        "block": hold("BL"),
        "crouch_block": hold("DOWN", "BL"),
        "high_punch": tap("HP"),
        "low_punch": tap("LP"),
        "high_kick": tap("HK"),
        "low_kick": tap("LK"),
        "uppercut": [({"DOWN"}, 8)] + tap("DOWN", "HP"),  # crouch first, or the game reads a standing punch
        "sweep": tap("AWAY", "LK"),
        "roundhouse": tap("AWAY", "HK"),
        "duck": hold("DOWN"),
        # Forward, forward + button. Checked against an idle Jax: 17, 17 and 20 damage.
        "fireball_high": [(pad("TOWARD"), 3), (set(), 3)] + tap("HP"),
        "fireball_low": [(pad("TOWARD"), 3), (set(), 3), (pad("TOWARD"), 3), (set(), 2)] + tap("LP"),
        "flying_kick": [(pad("TOWARD"), 3), (set(), 3)] + tap("TOWARD", "HK"),
    }[action]
    return [buttons for buttons, frames in plan for _ in range(frames)]


def sample(probs, temperature, rng=random):
    """Pick an option. 0 = always the top choice, 1 = Clef's raw distribution."""
    if temperature <= 0:
        return max(probs, key=probs.get)
    weights = {k: p ** (1 / temperature) for k, p in probs.items()}
    r = rng.random() * sum(weights.values())
    for k, w in weights.items():
        r -= w
        if r <= 0:
            return k
    return k


def bucket(px):
    # Bodies stop at ~45 px, every attack reaches at 70, nothing reaches past 94, rounds start ~148 apart.
    return "very close" if px < 60 else "close" if px < 80 else "mid" if px < 100 else "far"


# Opponent in each VeryEasy.LiuKang-NN save state, read off the health bars.
LADDER = {2: "Rayden", 3: "Kitana", 4: "Kung Lao", 5: "Baraka", 6: "Reptile", 7: "Johnny Cage", 8: "Scorpion",
          9: "Mileena", 10: "Sub-Zero", 11: "Liu Kang", 12: "Jade", 13: "Shang Tsung", 14: "Kintaro", 15: "Shao Kahn"}


def matchup(state_name):
    m = re.search(r"([A-Za-z]+)Vs([A-Za-z]+)", state_name)
    if m:
        return f"You are {m[1]} (player 1). The CPU is {m[2]}."
    m = re.search(r"\.([A-Za-z]+)-(\d+)$", state_name)  # VeryEasy.LiuKang-02 = Liu Kang's arcade ladder
    if m:
        return f"You are {m[1]} (player 1). The CPU is {LADDER.get(int(m[2]), 'unknown')}."
    return "You are player 1. The enemy is the CPU."


def build_state(history, state_name, variant="baseline"):
    """Game memory -> state for Clef. Returns a string for v2, dict for all others."""
    info, prev = history[-1], history[max(len(history) - 15, 0)]  # movement over the last 0.25 s
    dx = info["enemy_x_position"] - info["x_position"]
    ev = info["enemy_x_position"] - prev["enemy_x_position"]
    moving = "still" if abs(ev) < 2 else "toward me" if ev * dx < 0 else "away from me"

    def airborne(key):  # y_position is vertical speed, not height: 0 on the ground and for ~3 frames at the top
        return any(h[key] != 0 for h in history[-5:])

    me_hp = round(100 * info["health"] / MAX_HEALTH)
    en_hp = round(100 * info["enemy_health"] / MAX_HEALTH)

    if variant == "v2":
        def hw(pct):
            return "critical" if pct < 25 else "low" if pct < 50 else "medium" if pct < 75 else "high"
        dist = bucket(abs(dx))
        side = "right" if dx > 0 else "left"
        mv = moving.replace("me", "you")
        enemy_atk = any(h.get("enemy_attacking") for h in history[-3:])
        reaches = [a.replace("_", " ") for a, r in VARIANTS["tactics"]["reach"].items() if abs(dx) <= r]
        got_hit = info["health"] < history[0]["health"]
        hit_enemy = info["enemy_health"] < history[0]["enemy_health"]
        mu = re.sub(r" \(player 1\)", "", matchup(state_name))
        return "\n".join([
            mu,
            f"Distance: {dist}. Enemy side: {side}. Enemy moving: {mv}.",
            f"Enemy attacking now: {'yes' if enemy_atk else 'no'}. Enemy airborne: {'yes' if airborne('enemy_y_position') else 'no'}. You airborne: {'yes' if airborne('y_position') else 'no'}.",
            f"Your health: {hw(me_hp)}. Enemy health: {hw(en_hp)}. You got hit recently: {'yes' if got_hit else 'no'}. You hit the enemy recently: {'yes' if hit_enemy else 'no'}.",
            f"Attacks that reach: {', '.join(reaches) if reaches else 'none'}.",
        ])

    return {
        "matchup": matchup(state_name),
        "me": {
            "health_pct": me_hp,
            "airborne": airborne("y_position"),
            "rounds_won": info["rounds_won"],
        },
        "enemy": {
            "health_pct": en_hp,
            "airborne": airborne("enemy_y_position"),
            "moving": moving,
            "rounds_won": info["enemy_rounds_won"],
        },
        "enemy_side": "right" if dx > 0 else "left",
        "distance": bucket(abs(dx)),
        "distance_px": abs(dx),
        "attacks_in_reach": [a for a, r in VARIANTS[variant]["reach"].items() if abs(dx) <= r],
        "health_lead_pct": me_hp - en_hp,
        # No move history here: in testing it made Clef repeat its last move.
    } | ({
        "i_got_hit_recently": info["health"] < history[0]["health"],  # history covers 0.75 s
        "i_hit_enemy_recently": info["enemy_health"] < history[0]["enemy_health"],
    } if variant == "tactics" else {})


class Clef:
    def __init__(self, model, account, token):
        self.model = model
        self.path = f"/client/v4/accounts/{account}/ai/run/@cf/cloudflare/{model}"
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.local = threading.local()  # one kept-alive connection per worker: ~1.3 s -> ~0.3 s

    def ask(self, body):
        for attempt in (0, 1):
            conn = getattr(self.local, "conn", None) or http.client.HTTPSConnection("api.cloudflare.com", timeout=10)
            self.local.conn = conn
            try:
                conn.request("POST", self.path, json.dumps(body), self.headers)
                data = json.loads(conn.getresponse().read())
                break
            except (http.client.HTTPException, OSError):
                conn.close()
                self.local.conn = None
                if attempt:
                    raise
        if not data.get("success"):
            raise RuntimeError(json.dumps(data.get("errors"))[:300])
        return data["result"]


class Game:
    """State shared by the game loop, the Clef workers, and the web server."""

    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.frame_ready = threading.Condition()
        self.jpeg = None
        self.playing = False
        self.fighting = False
        self.state_name = ""
        self.variant = args.variant
        self.info = None
        self.history = deque(maxlen=45)  # last 0.75 s of memory: movement, jumps, recent hits
        self.plan = deque()
        self.last_seq = 0
        self.last_seq_sent = 0
        self.round_decisions = 0
        self.inflight = 0
        self.last_moves = deque(maxlen=12)
        self.decision = None
        self.score = {"clef": 0, "cpu": 0}
        self.stats = {"requests": 0, "tokens": 0, "stale": 0, "errors": 0, "last_error": None}
        self.latencies = deque(maxlen=200)
        self.decided_at = deque(maxlen=50)

    def on_frame(self, obs, info, frame_no):
        with self.lock:
            self.info = info
            self.history.append(info)
        if frame_no % 2 == 0:  # stream at 30 fps
            from PIL import Image

            buf = io.BytesIO()
            Image.fromarray(obs).save(buf, "JPEG", quality=80)
            with self.frame_ready:
                self.jpeg = buf.getvalue()
                self.frame_ready.notify_all()

    def next_buttons(self):
        with self.lock:
            return self.plan.popleft() if self.plan else set()

    def snapshot(self):
        with self.lock:
            if not (self.playing and self.fighting and self.history and self.inflight < self.args.max_inflight):
                return None
            self.inflight += 1
            self.last_seq_sent += 1
            state = build_state(list(self.history), self.state_name, self.variant)
            if self.variant == "script":  # decide from memory now; the 0.4 s delay comes in decide()
                state = {"script_action": script_policy(list(self.history), random)}
            return self.last_seq_sent, state, self.jpeg if self.args.frame else None, self.variant

    def apply(self, seq, state, answers, latency_ms, tokens):
        with self.lock:
            self.inflight -= 1
            self.stats["requests"] += 1
            self.stats["tokens"] += tokens
            self.latencies.append(latency_ms)
            if seq <= self.last_seq or not self.fighting:
                self.stats["stale"] += 1
                return
            self.last_seq = seq
            action_ans = answers["action"]
            probs = action_ans["probabilities"]
            dx = self.info["enemy_x_position"] - self.info["x_position"]
            if self.variant in ("tactics", "v2") and abs(dx) > max(REACH.values()):  # guard: CPU anti-airs long jumps
                probs = {k: p for k, p in probs.items() if k != "jump_in"}
            action = sample(probs, self.args.temperature)
            threat = answers.get("threat", {}).get("noul") if self.variant == "v2" else None
            overridden_by = None
            if threat is not None and threat > 0.7 and abs(dx) <= 100:
                action = "block"
                overridden_by = "threat"
            self.round_decisions += 1
            toward = "RIGHT" if dx > 0 else "LEFT"
            away = "LEFT" if toward == "RIGHT" else "RIGHT"
            self.plan = deque(plan_for(action, toward, away))
            self.last_moves.append(action)
            self.decided_at.append(time.time())
            self.decision = {
                "action": action,
                "probabilities": action_ans["probabilities"],
                "confidence": action_ans.get("confidence"),
                "latency_ms": round(latency_ms),
                "state": state,
            }
            if threat is not None:
                self.decision["threat"] = threat
            if overridden_by:
                self.decision["overridden_by"] = overridden_by

    def fail(self, error):
        with self.lock:
            self.inflight -= 1
            self.stats["errors"] += 1
            self.stats["last_error"] = str(error)[:300]

    def view(self):
        with self.lock:
            lat = sorted(self.latencies)
            neurons = self.stats["tokens"] * NEURONS_PER_M_INPUT[self.args.model] / 1e6
            now = time.time()
            return {
                "playing": self.playing,
                "fighting": self.fighting,
                "matchup": matchup(self.state_name),
                "state_name": self.state_name,
                "score": self.score,
                "memory": self.info,
                "decision": self.decision,
                "recent": list(self.last_moves),
                "actions": list(VARIANTS[self.variant]["actions"]),
                "variant": self.variant,
                "temperature": self.args.temperature,
                "cost": {
                    "model": self.args.model,
                    **self.stats,
                    "neurons": round(neurons, 1),
                    "usd": round(neurons * USD_PER_1K_NEURONS / 1000, 5),
                    "free_left": round(max(FREE_NEURONS_PER_DAY - neurons, 0)),
                    "free_per_day": FREE_NEURONS_PER_DAY,
                    "latency_median": round(statistics.median(lat)) if lat else None,
                    "latency_p95": round(lat[int(len(lat) * 0.95)]) if lat else None,
                    "decisions_per_s": round(sum(now - t < 5 for t in self.decided_at) / 5, 1),
                },
            }


INTTYPE = None  # set in main(): your ROM in integration/, or one imported into stable-retro


def integration():
    import stable_retro as retro

    if (HERE / "integration" / GAME / "rom.md").exists():
        retro.data.Integrations.add_custom_path(str(HERE / "integration"))
        return retro.data.Integrations.CUSTOM_ONLY
    return retro.data.Integrations.STABLE


def install_rom(path):
    """Use any intact Genesis MK2 dump (zip or bin): copy stable-retro's integration and pin it to this ROM."""
    import hashlib
    import shutil
    import struct
    import zipfile

    import stable_retro as retro

    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.lower().endswith((".bin", ".md", ".gen"))), None)
            if not name:
                raise SystemExit(f"No .bin, .md or .gen file inside {path.name}.")
            rom = z.read(name)
    else:
        rom = path.read_bytes()
    if rom[0x100:0x104] != b"SEGA" or b"MORTAL KOMBAT II" not in rom[0x120:0x190]:
        raise SystemExit("This is not a Sega Genesis Mortal Kombat II ROM (or it is in .smd format).")
    body = rom[0x200:]
    if sum(struct.unpack(f">{len(body) // 2}H", body)) & 0xFFFF != struct.unpack(">H", rom[0x18E:0x190])[0]:
        raise SystemExit("The ROM checksum is wrong. The file is damaged or modified.")
    src = Path(retro.data.get_file_path(GAME, "rom.sha", retro.data.Integrations.STABLE)).parent
    dst = HERE / "integration" / GAME
    shutil.copytree(src, dst, dirs_exist_ok=True)
    (dst / "rom.md").write_bytes(rom)
    (dst / "rom.sha").write_text(hashlib.sha1(rom).hexdigest() + "\n")
    print(f"ROM OK (checksum matches). Installed to {dst}. Now run: uv run mk2_clef.py")


def make_env(state):
    import stable_retro as retro

    return retro.make(GAME, state=state, inttype=INTTYPE, render_mode=None, use_restricted_actions=retro.Actions.ALL)


def press(env, buttons):
    import numpy as np

    return env.step(np.array([b in buttons for b in env.buttons], dtype=np.int8))


def game_loop(game, env, schedule, repeat=True, log=None):
    """Play each (state, variant) round in order. With a log path, append one JSON line per round."""
    for i in range(10**9 if repeat else len(schedule)):
        name, variant = schedule[i % len(schedule)]
        env.load_state(name, INTTYPE)
        env.reset()
        with game.lock:
            game.state_name, game.variant, game.plan, game.decision = name, variant, deque(), None
            game.history.clear()
            game.last_moves.clear()
            game.last_seq = game.last_seq_sent  # answers still in flight belong to the last round
            game.round_decisions, tokens0, errors0 = 0, game.stats["tokens"], game.stats["errors"]
        frame_no, end_frame, first = 0, None, None
        while end_frame is None or frame_no - end_frame < 3 * FPS:  # after the round, show the result 3 s
            t0 = time.perf_counter()
            if not game.playing:
                time.sleep(0.05)
                continue
            # Decide from frame 0: intro length differs per save state, and idling during a fight is worse.
            game.fighting = end_frame is None
            buttons = game.next_buttons() if game.fighting else set()
            if game.variant == "v2" and game.fighting:
                with game.lock:
                    hist = list(game.history)
                if reflex_block(hist):
                    buttons = PAD["BL"]
                    with game.lock:
                        game.plan.clear()
            obs, _, term, trunc, info = press(env, buttons)
            read_flags(env, info)
            game.on_frame(obs, info, frame_no)
            first = first or info
            frame_no += 1
            if end_frame is None and (term or trunc or frame_no > 100 * FPS):
                won = info["enemy_health"] < info["health"]
                with game.lock:
                    game.fighting, game.plan = False, deque()
                    game.score["clef" if won else "cpu"] += 1
                    result = {"state": name, "variant": variant, "won": won, "seconds": round(frame_no / FPS, 1),
                              "damage_dealt": first["enemy_health"] - info["enemy_health"],
                              "damage_taken": first["health"] - info["health"],
                              "decisions": game.round_decisions, "tokens": game.stats["tokens"] - tokens0,
                              "errors": game.stats["errors"] - errors0}
                if log:
                    with open(log, "a") as f:
                        f.write(json.dumps(result) + "\n")
                    print(f"{i + 1}/{len(schedule)} {name:<22} {variant:<9} {'WIN ' if won else 'loss'} "
                          f"dealt {result['damage_dealt']:>3} taken {result['damage_taken']:>3} {result['seconds']:>5}s"
                          + (f"  {result['errors']} Clef errors (quota?)" if result["errors"] else ""))
                end_frame = frame_no
            time.sleep(max(0, 1 / FPS - (time.perf_counter() - t0)))


def brain_loop(game, clef, args):
    pool = ThreadPoolExecutor(args.max_inflight)

    def decide(seq, state, jpeg, variant):
        if variant == "script":
            time.sleep(0.4)
            return game.apply(seq, state, {"action": {"probabilities": {state["script_action"]: 1.0}}}, 400, 0)
        if variant == "random":
            time.sleep(0.4)
            return game.apply(seq, state, {"action": {"probabilities": dict.fromkeys(ACTIONS, 1 / len(ACTIONS))}}, 400, 0)
        body = {"model": clef.model, "state": state, "questions": questions(variant)}
        if jpeg:
            body["images"] = ["data:image/jpeg;base64," + base64.b64encode(jpeg).decode()]
        t0 = time.perf_counter()
        try:
            result = clef.ask(body)
        except Exception as e:  # network or quota errors: show them in the cost panel, keep playing
            return game.fail(e)
        game.apply(seq, state, result["answers"], (time.perf_counter() - t0) * 1000,
                   result.get("usage", {}).get("input_tokens", 0))

    while True:
        time.sleep(args.interval)
        snap = game.snapshot()
        if snap:
            pool.submit(decide, *snap)


def serve(game, port):
    page = (HERE / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                return self.send(page, "text/html; charset=utf-8")
            if self.path == "/state":
                return self.send(json.dumps(game.view()).encode(), "application/json")
            if self.path == "/video":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                last = None
                try:
                    while True:
                        with game.frame_ready:
                            game.frame_ready.wait_for(lambda: game.jpeg is not last, timeout=1)
                            last = jpeg = game.jpeg
                        if jpeg:
                            self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpeg))
                            self.wfile.write(jpeg + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    return
            self.send_error(404)

        def do_POST(self):
            if self.path == "/toggle":
                game.playing = not game.playing
                return self.send(b"{}", "application/json")
            self.send_error(404)

    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def calibrate(env, states):
    """Play each move once from the first fight and save one labelled frame per move, to check PAD."""
    from PIL import Image, ImageDraw

    moves = VARIANTS["tactics"]["actions"]  # superset of the baseline moves
    sheet = Image.new("RGB", (320 * 4, 224 * ((len(moves) + 3) // 4)), "white")
    draw = ImageDraw.Draw(sheet)
    for i, action in enumerate(moves):
        env.load_state(states[0], INTTYPE)
        env.reset()
        for _ in range(INTRO_FRAMES + 10):
            info = press(env, set())[4]
        toward = "RIGHT" if info["enemy_x_position"] > info["x_position"] else "LEFT"
        away = "LEFT" if toward == "RIGHT" else "RIGHT"
        plan = plan_for(action, toward, away)
        for buttons in (plan + [set()] * 3)[: len(plan) + 3]:  # attacks look clearest ~8 frames in
            obs = press(env, buttons)[0]
        x, y = i % 4 * 320, i // 4 * 224
        sheet.paste(Image.fromarray(obs), (x, y))
        draw.rectangle((x, y, x + 110, y + 14), fill="black")
        draw.text((x + 4, y + 2), action, fill="yellow")
    (HERE / "calibration").mkdir(exist_ok=True)
    sheet.save(HERE / "calibration" / "moves.png")
    print(f"Saved {HERE / 'calibration' / 'moves.png'}. If a move looks wrong, fix PAD or plan_for in mk2_clef.py.")



def summarize(log, model="clef-flash"):
    rows = [json.loads(line) for line in open(log)]
    print(f"\n{'variant':<20}{'rounds':>7}{'wins':>6}{'dealt':>8}{'taken':>8}{'margin ±95%':>14}{'secs':>7}{'neurons':>9}")
    for v in dict.fromkeys(x["variant"] for x in rows):
        r = [x for x in rows if x["variant"] == v]
        avg = lambda k: sum(x[k] for x in r) / len(r)  # noqa: E731
        margins = [x["damage_dealt"] - x["damage_taken"] for x in r]
        ci = 1.96 * statistics.stdev(margins) / len(r) ** 0.5 if len(r) > 1 else 0
        neurons = sum(x["tokens"] for x in r) * NEURONS_PER_M_INPUT[model] / 1e6
        print(f"{v:<20}{len(r):>7}{sum(x['won'] for x in r):>6}{avg('damage_dealt'):>8.1f}"
              f"{avg('damage_taken'):>8.1f}{statistics.mean(margins):>8.1f} ±{ci:<4.0f}{avg('seconds'):>7.1f}{neurons:>9.0f}")
    by_fight = {}
    for x in rows:
        by_fight.setdefault(x["state"], {}).setdefault(x["variant"], []).append(x["damage_dealt"])
    for v1, v2 in [("tactics", "baseline"), ("v2", "tactics")]:
        pairs = [(sum(f[v1]) / len(f[v1]), sum(f[v2]) / len(f[v2]))
                 for f in by_fight.values() if v1 in f and v2 in f]
        if pairs:
            print(f"{v1} dealt more damage than {v2} in {sum(a > b for a, b in pairs)} of {len(pairs)} fights, "
                  f"less in {sum(a < b for a, b in pairs)}.")
    if any(x.get("errors") for x in rows):
        print(f"WARNING: {sum(x.get('errors', 0) for x in rows)} Clef errors. Rounds with errors are not a fair test.")


def sim_round(env, state_name, arm_spec, policy_fn, use_reflex, lag, rng):
    """Run one headless unpaced round. No sleep, no web server. Returns result dict."""
    env.load_state(state_name, INTTYPE)
    env.reset()
    history = deque(maxlen=45)
    plan = deque()
    pending = []  # [(apply_at_frame, action)]
    frame_no, first_info, decisions = 0, None, 0
    while True:
        toward, away = "RIGHT", "LEFT"
        if history:
            dx_now = history[-1].get("enemy_x_position", 200) - history[-1].get("x_position", 0)
            toward = "RIGHT" if dx_now > 0 else "LEFT"
            away = "LEFT" if toward == "RIGHT" else "RIGHT"
        while pending and pending[0][0] <= frame_no:
            _, action = pending.pop(0)
            plan = deque(plan_for(action, toward, away))
        if use_reflex and history and reflex_block(list(history)):
            buttons = PAD["BL"]
            plan.clear()
        else:
            buttons = plan.popleft() if plan else set()
        obs, _, term, trunc, info = press(env, buttons)
        read_flags(env, info)
        history.append(info)
        first_info = first_info or info
        frame_no += 1
        if frame_no % DECISION_EVERY == 0:
            pending.append((frame_no + lag, policy_fn(list(history), rng, state_name)))
            decisions += 1
        if term or trunc or frame_no > 100 * FPS:
            won = info["enemy_health"] < info["health"]
            return {"state": state_name, "variant": arm_spec, "won": won,
                    "seconds": round(frame_no / FPS, 1),
                    "damage_dealt": first_info["enemy_health"] - info["enemy_health"],
                    "damage_taken": first_info["health"] - info["health"],
                    "decisions": decisions}


def summarize_sim(rows):
    arms = list(dict.fromkeys(r["variant"] for r in rows))
    print(f"\n{'arm':<24}{'rds':>5}{'wins':>5}{'win%':>6}{'dealt':>7}{'±ci':>7}{'taken':>7}{'secs':>7}")
    for arm in arms:
        r = [x for x in rows if x["variant"] == arm]
        n = len(r)
        wins = sum(x["won"] for x in r)
        def _ci(vals):
            return 1.96 * statistics.stdev(vals) / len(vals) ** 0.5 if len(vals) > 1 else 0.0
        dealt = [x["damage_dealt"] for x in r]
        taken = [x["damage_taken"] for x in r]
        print(f"{arm:<24}{n:>5}{wins:>5}{100*wins/n:>5.0f}%{statistics.mean(dealt):>7.1f}"
              f"{_ci(dealt):>7.1f}{statistics.mean(taken):>7.1f}{statistics.mean([x['seconds'] for x in r]):>7.1f}")
    # Paired net-damage comparisons per fight (mean over repeats). Net = dealt - taken.
    print()
    by_fight = {}
    for x in rows:
        by_fight.setdefault(x["state"], {}).setdefault(x["variant"], []).append(
            x["damage_dealt"] - x["damage_taken"])
    def _paired(a_arm, b_arm, label):
        diffs = [statistics.mean(f[a_arm]) - statistics.mean(f[b_arm])
                 for f in by_fight.values() if a_arm in f and b_arm in f]
        if not diffs:
            return
        n, mean_d = len(diffs), statistics.mean(diffs)
        ci = 1.96 * statistics.stdev(diffs) / n ** 0.5 if n > 1 else 0.0
        sig = "not significant (CI crosses 0)" if mean_d - ci < 0 < mean_d + ci else (
            "favors A" if mean_d > 0 else "favors B")
        print(f"{label}: {a_arm} beat {b_arm} in {sum(d > 0 for d in diffs)} of {n} fights, "
              f"mean net Δ {mean_d:+.1f} ± {ci:.1f} → {sig}")
    _paired("random+reflex@24", "random@24", "H1 reflex")
    _paired("script@24", "random@24", "H3 script")
    _paired("script+reflex@24", "script@24", "H3 script+reflex")
    _paired("random@0", "random@24", "H2 lag random 0vs24")
    _paired("script@0", "script@24", "H2 lag script 0vs24")


def run_sim(args, env, all_states):
    """Headless unpaced experiment: no sleep, no web server, no Clef calls."""
    ladder = [s for s in all_states if s.startswith(args.ladder)][: args.fights]
    tables = {}
    if "clef" in args.sim:
        load_env_file()
        clef = Clef(args.model, os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"])
        tables = {name: ClefTable(clef, args.temperature, name) for name in TABLE_PROMPTS}
    arms = [parse_arm(a.strip(), tables) for a in args.sim.split(",") if a.strip()]
    log = HERE / "ab" / f"sim-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    log.parent.mkdir(exist_ok=True)
    total = len(ladder) * args.repeats * len(arms)
    print(f"Sim: {len(arms)} arms × {len(ladder)} fights × {args.repeats} repeats = {total} rounds. Log: {log}")
    rows, done, t0 = [], 0, time.perf_counter()
    for repeat in range(args.repeats):
        for fi, state_name in enumerate(ladder):
            for arm_spec, policy_fn, use_reflex, lag in arms:
                rng = random.Random(f"{repeat}-{fi}")  # same seed per fight so arms see comparable randomness
                result = sim_round(env, state_name, arm_spec, policy_fn, use_reflex, lag, rng)
                rows.append(result)
                with open(log, "a") as f:
                    f.write(json.dumps(result) + "\n")
                done += 1
                print(f"{done}/{total} {state_name:<22} {arm_spec:<22} {'WIN ' if result['won'] else 'loss'} "
                      f"dealt {result['damage_dealt']:>3} taken {result['damage_taken']:>3} {result['seconds']:>5}s")
    elapsed = time.perf_counter() - t0
    print(f"\n{done} rounds in {elapsed:.0f}s ({60 * done / elapsed:.1f} rounds/min)")
    summarize_sim(rows)
    if tables:
        tokens = sum(t.tokens for t in tables.values())
        print(f"Clef: {tokens:,} new input tokens, about "
              f"{tokens * NEURONS_PER_M_INPUT[args.model] / 1e6:.0f} neurons (cached answers are free).")


def load_env_file():
    path = HERE / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="clef-flash", choices=list(NEURONS_PER_M_INPUT))
    p.add_argument("--state", help="one save state to replay, for example Level1.LiuKangVsJax (default: cycle all)")
    p.add_argument("--temperature", type=float, default=0.3, help="0 = always the top choice, 1 = raw distribution")
    p.add_argument("--interval", type=float, default=0.12, help="seconds between Clef requests")
    p.add_argument("--max-inflight", type=int, default=3, help="most Clef requests in flight at once")
    p.add_argument("--frame", action="store_true", help="also send the current frame to Clef (about 75 more tokens)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--calibrate", action="store_true", help="save one frame per button to calibration/ and exit")
    p.add_argument("--rom", help="install your Genesis MK2 ROM (zip or bin) and exit")
    p.add_argument("--variant", default="baseline", choices=list(VARIANTS), help="prompt, moves and state Clef gets")
    p.add_argument("--ab", type=int, metavar="N", help="A/B test: play each VeryEasy Liu Kang fight N times per "
                   "variant, headless, log to ab/, print results")
    p.add_argument("--arms", default=",".join(VARIANTS), help="comma list of variants for A/B (default all)")
    p.add_argument("--fights", type=int, default=14, help="A/B: first N of the 14 ladder fights (saves quota)")
    p.add_argument("--sim", metavar="ARMS", help="free unpaced test of code-only arms, e.g. "
                   "random@0,script@24,clef@0 (arm: random, script, or clef; +reflex adds the block reflex; @N = latency frames)")
    p.add_argument("--repeats", type=int, default=3, help="sim: rounds per fight per arm")
    p.add_argument("--ladder", default="VeryEasy.LiuKang", help="sim: save-state prefix, e.g. LiuKangVs for the "
                   "15 VeryHard fights (held out: prompts were tuned on VeryEasy)")
    args = p.parse_args()
    if args.rom:
        return install_rom(args.rom)

    import stable_retro as retro

    global INTTYPE
    INTTYPE = integration()
    level = {"VeryEasy": 0, "Level1": 1}  # easiest CPU first, VeryHard last
    all_states = sorted((s for s in retro.data.list_states(GAME, INTTYPE) if not s.endswith(".2P")),
                        key=lambda s: (level.get(s.split(".")[0], 2), s))
    states = [args.state] if args.state else all_states
    try:
        env = make_env(states[0])
    except FileNotFoundError:
        raise SystemExit("Mortal Kombat II (Genesis) ROM not installed. Run: uv run mk2_clef.py --rom roms/<your file>.zip")
    if args.calibrate:  # Jax walks in slowly here, so each move is easy to see
        return calibrate(env, [args.state or "Level1.LiuKangVsJax"])
    if args.sim:  # free and unpaced: no Clef, no browser, no credentials needed
        return run_sim(args, env, all_states)

    load_env_file()
    account, token = os.environ.get("CLOUDFLARE_ACCOUNT_ID"), os.environ.get("CLOUDFLARE_API_TOKEN")
    if not (account and token):
        raise SystemExit("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env (see .env.example).")

    game = Game(args)
    threading.Thread(target=brain_loop, args=(game, Clef(args.model, account, token), args), daemon=True).start()
    if args.ab:
        # Same fight back to back for each variant, order flipped every fight, so time and latency drift cancel out.
        ladder = [s for s in all_states if s.startswith("VeryEasy.LiuKang")][: args.fights]
        arms = [v for v in args.arms.split(",") if v in VARIANTS]
        schedule = [(s, v) for n in range(args.ab) for k, s in enumerate(ladder)
                    for v in (arms if (n + k) % 2 == 0 else arms[::-1])]
        log = HERE / "ab" / f"results-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        log.parent.mkdir(exist_ok=True)
        clef_rounds = sum(v != "random" for _, v in schedule)
        print(f"A/B: {len(schedule)} rounds, about {clef_rounds * 20 * 14:,} neurons. Log: {log}")
        game.playing = True
        threading.Thread(target=serve, args=(game, args.port), daemon=True).start()  # watch or record the run
        print(f"Watch: http://localhost:{args.port}")
        game_loop(game, env, schedule, repeat=False, log=log)
        return summarize(log, args.model)
    threading.Thread(target=game_loop, args=(game, env, [(s, args.variant) for s in states]), daemon=True).start()
    url = f"http://localhost:{args.port}"
    print(f"Open {url} and press Play. Ctrl+C to stop.")
    threading.Timer(1, webbrowser.open, [url]).start()
    try:
        serve(game, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
