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

QUESTIONS = {
    "action": {
        "type": "choice",
        "instructions": "You are player 1 in Mortal Kombat II. Pick the next move that helps you win the round. "
        "Attack only with a move in attacks_in_reach. If none reach, approach, jump in, or block.",
        "criteria": ACTIONS,
    }
}


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


def matchup(state_name):
    m = re.search(r"([A-Za-z]+)Vs([A-Za-z]+)", state_name)
    if m:
        return f"You are {m[1]} (player 1). The CPU is {m[2]}."
    m = re.search(r"\.([A-Za-z]+)-\d+$", state_name)  # VeryEasy.LiuKang-02 = Liu Kang's arcade ladder
    return f"You are {m[1]} (player 1). The enemy is the CPU." if m else "You are player 1. The enemy is the CPU."


def build_state(history, state_name):
    """Game memory -> the JSON state that Clef reads. history = memory of the last frames, oldest first."""
    info, prev = history[-1], history[0]
    dx = info["enemy_x_position"] - info["x_position"]
    ev = info["enemy_x_position"] - prev["enemy_x_position"]
    moving = "still" if abs(ev) < 2 else "toward me" if ev * dx < 0 else "away from me"

    def airborne(key):  # y_position is vertical speed, not height: 0 on the ground and for ~3 frames at the top
        return any(h[key] != 0 for h in history[-5:])

    me_hp = round(100 * info["health"] / MAX_HEALTH)
    en_hp = round(100 * info["enemy_health"] / MAX_HEALTH)
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
        "attacks_in_reach": [a for a, r in REACH.items() if abs(dx) <= r],
        "health_lead_pct": me_hp - en_hp,
        # No move history here: in testing it made Clef repeat its last move.
    }


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
        self.info = None
        self.history = deque(maxlen=15)  # last 15 frames of memory: enemy movement and jumps
        self.plan = deque()
        self.last_seq = 0
        self.last_seq_sent = 0
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
            if not (self.playing and self.fighting and self.info and self.inflight < self.args.max_inflight):
                return None
            self.inflight += 1
            self.last_seq_sent += 1
            state = build_state(list(self.history), self.state_name)
            return self.last_seq_sent, state, self.jpeg if self.args.frame else None

    def apply(self, seq, state, answer, latency_ms, tokens):
        with self.lock:
            self.inflight -= 1
            self.stats["requests"] += 1
            self.stats["tokens"] += tokens
            self.latencies.append(latency_ms)
            if seq <= self.last_seq or not self.fighting:
                self.stats["stale"] += 1
                return
            self.last_seq = seq
            action = sample(answer["probabilities"], self.args.temperature)
            toward = "RIGHT" if self.info["enemy_x_position"] > self.info["x_position"] else "LEFT"
            away = "LEFT" if toward == "RIGHT" else "RIGHT"
            self.plan = deque(plan_for(action, toward, away))
            self.last_moves.append(action)
            self.decided_at.append(time.time())
            self.decision = {
                "action": action,
                "probabilities": answer["probabilities"],
                "confidence": answer.get("confidence"),
                "latency_ms": round(latency_ms),
                "state": state,
            }

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
                "actions": list(ACTIONS),
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


def game_loop(game, env, states):
    for i in range(10**9):
        name = states[i % len(states)]
        env.load_state(name, INTTYPE)
        env.reset()
        with game.lock:
            game.state_name, game.plan, game.decision = name, deque(), None
            game.history.clear()
            game.last_moves.clear()
        frame_no, end_frame = 0, None
        while end_frame is None or frame_no - end_frame < 3 * FPS:  # after the round, show the result 3 s
            t0 = time.perf_counter()
            if not game.playing:
                time.sleep(0.05)
                continue
            # Decide from frame 0: intro length differs per save state, and idling during a fight is worse.
            game.fighting = end_frame is None
            obs, _, term, trunc, info = press(env, game.next_buttons() if game.fighting else set())
            game.on_frame(obs, info, frame_no)
            frame_no += 1
            if end_frame is None and (term or trunc or frame_no > 100 * FPS):
                with game.lock:
                    game.fighting, game.plan = False, deque()
                    game.score["clef" if info["enemy_health"] < info["health"] else "cpu"] += 1
                end_frame = frame_no
            time.sleep(max(0, 1 / FPS - (time.perf_counter() - t0)))


def brain_loop(game, clef, args):
    pool = ThreadPoolExecutor(args.max_inflight)

    def decide(seq, state, jpeg):
        body = {"model": clef.model, "state": state, "questions": QUESTIONS}
        if jpeg:
            body["images"] = ["data:image/jpeg;base64," + base64.b64encode(jpeg).decode()]
        t0 = time.perf_counter()
        try:
            result = clef.ask(body)
        except Exception as e:  # network or quota errors: show them in the cost panel, keep playing
            return game.fail(e)
        game.apply(seq, state, result["answers"]["action"], (time.perf_counter() - t0) * 1000,
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

    sheet = Image.new("RGB", (320 * 4, 224 * 4), "white")
    draw = ImageDraw.Draw(sheet)
    for i, action in enumerate(ACTIONS):
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

    load_env_file()
    account, token = os.environ.get("CLOUDFLARE_ACCOUNT_ID"), os.environ.get("CLOUDFLARE_API_TOKEN")
    if not (account and token):
        raise SystemExit("Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env (see .env.example).")

    game = Game(args)
    threading.Thread(target=game_loop, args=(game, env, states), daemon=True).start()
    threading.Thread(target=brain_loop, args=(game, Clef(args.model, account, token), args), daemon=True).start()
    url = f"http://localhost:{args.port}"
    print(f"Open {url} and press Play. Ctrl+C to stop.")
    threading.Timer(1, webbrowser.open, [url]).start()
    try:
        serve(game, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
