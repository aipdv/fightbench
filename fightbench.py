# /// script
# requires-python = ">=3.10"
# dependencies = ["stable-retro==1.0.1", "pillow"]
# ///
"""FightBench edition mk2-liukang-v1: Mortal Kombat II (Genesis), Liu Kang vs the CPU, from game memory.

  uv run fightbench.py smoke                            # script and random on fight 0, played twice (free)
  uv run fightbench.py table --policy clef              # policy-table submission (Clef reads ab/clef-cache.json only)
  uv run fightbench.py live --policy my.py:MyPolicy     # play in real time, write a live submission
  uv run fightbench.py replay submissions/<file>.json   # maintainer: score a submission, refresh site/board.json
  uv run fightbench.py replay script                    # the floor row on the full ladder (150 rounds)

There is no prompt flag and no temperature flag: the edition file fixes both.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import re
import statistics
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import mk2_clef as core
from mk2_clef import plan_for, sample, script_policy, situation

HERE = Path(__file__).parent
EDITION_FILE = HERE / "edition" / "mk2-liukang-v1.json"
ED = json.loads(EDITION_FILE.read_text())
EDITION, SITUATIONS, MOVES, LIVE = ED["edition"], ED["situations"], ED["moves"], ED["live"]
MOVE_IDS = list(MOVES)
LADDER = [f["state"] for f in ED["ladder"]]
PROMPT_SHA256 = hashlib.sha256(EDITION_FILE.read_bytes()).hexdigest()
MIN_GAP_FRAMES = math.ceil(LIVE["min_gap_s"] * core.FPS)
VERIFIED, SUBMISSIONS, BOARD = HERE / "results" / "verified", HERE / "submissions", HERE / "site" / "board.json"


class Policy:
    """A model adapter. Return {situation_id: {move_id: probability}} for every situation asked.
    The policy table asks all 8 situations once per fight. Live asks only the current one."""
    name: str = "unnamed"
    version: str = ""

    def distributions(self, fight_id: str, state_line: str, situations: dict, moves: dict) -> dict:
        raise NotImplementedError


class ClefFlash(Policy):
    """Posts the same Workers AI choice body as mk2_clef.ClefTable. Without a client it reads the cache only."""
    name, version = "clef-flash", "@cf/cloudflare/clef-flash"

    def __init__(self, clef=None):
        self.clef = clef
        path = HERE / "ab" / "clef-cache.json"
        self.cache = json.loads(path.read_text()) if path.exists() else {}

    def distributions(self, fight_id, state_line, situations, moves):
        body = {"model": self.name, "state": state_line,
                "questions": {k: {"type": "choice", "instructions": ED["question"].format(situation=d),
                                  "criteria": moves} for k, d in situations.items()}}
        key = json.dumps(body, sort_keys=True)
        if key in self.cache:
            return self.cache[key]
        if not self.clef:
            raise LookupError(f"{fight_id}: not in ab/clef-cache.json. The table command does not call Workers AI.")
        return {k: a["probabilities"] for k, a in self.clef.ask(body)["answers"].items()}


def load_policy(spec, live=False):
    if spec == "clef":
        if not live:
            return ClefFlash()
        core.load_env_file()
        return ClefFlash(core.Clef("clef-flash", os.environ["CLOUDFLARE_ACCOUNT_ID"], os.environ["CLOUDFLARE_API_TOKEN"]))
    path, cls = spec.rsplit(":", 1)
    mod_spec = importlib.util.spec_from_file_location("submitted_policy", path)
    mod = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(mod)
    return getattr(mod, cls)()


def state_line(fight):
    return core.matchup(fight).replace(" (player 1)", "")


def seeded(repeat, fight_index):
    rng = random.Random()
    rng.seed(ED["seed"].format(repeat=repeat, fight_index=fight_index), version=2)
    return rng


def slug(text):
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")


def row_id(sub, smoke):
    """<model>--<model_version>__<track>__<edition>[__smoke]. The floor rows' version 'harness' is left out."""
    version = slug(sub.get("model_version") or "")
    model = slug(sub["model"]) + (f"--{version}" if version not in ("", "harness") else "")
    return f"{model}__{sub['track']}__{EDITION}{'__smoke' if smoke else ''}"


# --- Validation: reject, never repair -------------------------------------------------------------------------------

def check_dist(dist):
    if not isinstance(dist, dict) or set(dist) != set(MOVE_IDS):
        raise ValueError("a distribution needs exactly the 17 move ids")
    vals = list(dist.values())
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0 for v in vals):
        raise ValueError("every probability must be finite and >= 0")
    if not 0.999 <= sum(vals) <= 1.001:
        raise ValueError(f"probabilities sum to {sum(vals)}, not 1")


def check_tables(tables):
    """True for an official file (all 15 fights), False for a smoke file (fight 0 only). Raises otherwise."""
    official = set(tables) == set(LADDER)
    if not official and set(tables) != {LADDER[0]}:
        raise ValueError("tables need all 15 fights, or only the smoke fight " + LADDER[0])
    for fight, table in tables.items():
        if set(table) != set(SITUATIONS):
            raise ValueError(f"{fight}: needs all 8 situations")
        for sit, dist in table.items():
            try:
                check_dist(dist)
            except ValueError as e:
                raise ValueError(f"{fight} {sit}: {e}") from None
    return official


def check_live(sub):
    """True for an official log (15 fights x 10 repeats), False for the smoke round. Raises otherwise.
    These checks tie the log to the game. They do not prove which model answered."""
    if sub.get("interval_s") != LIVE["interval_s"] or sub.get("max_inflight") != LIVE["max_inflight"]:
        raise ValueError(f"interval_s must be {LIVE['interval_s']} and max_inflight {LIVE['max_inflight']}")
    keys = sorted((r["fight"], r["repeat"]) for r in sub["rounds"])
    official = keys == sorted((f, n) for f in LADDER for n in range(ED["repeats"]))
    if not official and keys != [(LADDER[0], 0)]:
        raise ValueError("rounds need every fight x repeats 0..9, or only the smoke round")
    def whole(v):
        return isinstance(v, int) and not isinstance(v, bool)

    for r in sub["rounds"]:
        where = f"{r['fight']} repeat {r['repeat']}"
        if r.get("fps", 0) < LIVE["min_fps"]:
            raise ValueError(f"{where}: ran at {r.get('fps')} fps, below {LIVE['min_fps']}")
        open_, last, last_apply, next_apply = [], None, -1, None
        for d in r["decisions"]:
            if not whole(d["frame"]) or not (whole(d["done_frame"]) or d["done_frame"] is None and d["dropped"]):
                raise ValueError(f"{where} frame {d['frame']}: frame and done_frame must be whole frame numbers")
            if d["situation"] not in SITUATIONS or (d["action"] is not None and d["action"] not in MOVES):
                raise ValueError(f"{where} frame {d['frame']}: unknown situation or move")
            if last is not None and d["frame"] - last < MIN_GAP_FRAMES:
                raise ValueError(f"{where} frame {d['frame']}: request starts closer than {LIVE['min_gap_s']} s")
            open_ = [f for f in open_ if f is None or f > d["frame"]]
            if len(open_) >= LIVE["max_inflight"]:
                raise ValueError(f"{where} frame {d['frame']}: more than {LIVE['max_inflight']} requests in flight")
            if d["action"] is not None and not d["dropped"]:
                if not d["apply_frame"] == d["done_frame"] > d["frame"] or d["apply_frame"] < last_apply:
                    raise ValueError(f"{where} frame {d['frame']}: apply_frame must be the frame the answer arrived, "
                                     "after the request, and not before an older answer's apply_frame")
                last_apply = d["apply_frame"]
            open_.append(d["done_frame"])
            last = d["frame"]
        for d in reversed(r["decisions"]):  # the seq rule: a dropped answer arrived after a newer one was applied
            if d["action"] is not None and not d["dropped"]:
                next_apply = d["apply_frame"]
            elif d["dropped"] and d["done_frame"] is not None and (next_apply is None or next_apply > d["done_frame"]):
                raise ValueError(f"{where} frame {d['frame']}: dropped, but no newer answer was applied by then")
    return official


# --- Play ------------------------------------------------------------------------------------------------------------

def random_policy(history, rng, state_name=None):
    return rng.choice(MOVE_IDS)  # the 17 edition moves, in edition order


def table_policy(tables):
    def pick(history, rng, state_name):
        dist = tables[state_name][situation(history)]
        return sample({m: dist[m] for m in MOVE_IDS}, ED["temperature"], rng)  # edition order: one seed, one pick
    return pick


def table_round(env, fight, policy_fn, rng):
    """One round on the frozen grid: mk2_clef.sim_round, lag 0, no reflex. Logs each decision."""
    log = []

    def logged(history, rng, state_name):
        action = policy_fn(history, rng, state_name)
        frame = ED["decision_every_frames"] * (len(log) + 1)  # sim_round decides every 15 frames, lag 0 applies at once
        log.append({"frame": frame, "situation": situation(history), "action": action, "apply_frame": frame})
        return action

    r = core.sim_round(env, fight, "", logged, False, ED["lag_frames"], rng)
    return {k: r[k] for k in ("damage_dealt", "damage_taken", "won", "seconds")} | {"decisions": log}


def live_round(env, fight, on_frame, pace=False):
    """sim_round's frame loop, but on_frame(frame_no, history) may start a move on any frame.
    pace=True holds 60 fps on the wall clock (live play). Replay runs unpaced."""
    env.load_state(fight, core.INTTYPE)
    env.reset()
    history, plan = deque(maxlen=45), deque()
    frame_no, first, t0 = 0, None, time.perf_counter()
    while True:
        action = on_frame(frame_no, list(history))
        if action:
            dx = history[-1]["enemy_x_position"] - history[-1]["x_position"] if history else 1
            toward, away = ("RIGHT", "LEFT") if dx > 0 else ("LEFT", "RIGHT")
            plan = deque(plan_for(action, toward, away))
        _, _, term, trunc, info = core.press(env, plan.popleft() if plan else set())
        core.read_flags(env, info)
        history.append(info)
        first = first or info
        frame_no += 1
        if pace:
            time.sleep(max(0.0, t0 + frame_no / core.FPS - time.perf_counter()))
        if term or trunc or frame_no > ED["max_frames"]:
            return {"damage_dealt": first["enemy_health"] - info["enemy_health"],
                    "damage_taken": first["health"] - info["health"],
                    "won": info["enemy_health"] < info["health"], "seconds": round(frame_no / core.FPS, 1),
                    "fps": round(frame_no / (time.perf_counter() - t0), 1)}


def play_live(env, policy, fight, rng):
    """Ask the model about the current situation every 0.12 s, at most 3 requests in flight.
    A late answer is dropped once a newer one has been applied. An error presses nothing."""
    pool = ThreadPoolExecutor(LIVE["max_inflight"])
    decisions, inflight, last_applied, next_at = [], {}, -1, 0.0
    line = state_line(fight)

    def ask(sit):
        t = time.perf_counter()
        try:
            return policy.distributions(fight, line, {sit: SITUATIONS[sit]}, MOVES)[sit], None, t
        except Exception as e:  # noqa: BLE001 - any model failure is logged, never replaced by another policy
            return None, f"{type(e).__name__}: {e}"[:200], t

    def on_frame(frame_no, history):
        nonlocal last_applied, next_at
        now, action = time.perf_counter(), None
        for seq in sorted(s for s, f in inflight.items() if f.done()):
            dist, error, t = inflight.pop(seq).result()
            d = decisions[seq]
            d.update(latency_ms=round(1000 * (now - t)), done_frame=frame_no)
            if error is None:
                try:
                    check_dist(dist)
                except ValueError as e:
                    error = str(e)
            if error:
                d["error"] = error
            elif seq <= last_applied:
                d["dropped"] = True
            else:
                action = sample({m: dist[m] for m in MOVE_IDS}, ED["temperature"], rng)
                d.update(action=action, apply_frame=frame_no)
                last_applied = seq
        if (history and now >= next_at and len(inflight) < LIVE["max_inflight"]
                and (not decisions or frame_no - decisions[-1]["frame"] >= MIN_GAP_FRAMES)):
            sit = situation(history)
            decisions.append({"frame": frame_no, "situation": sit, "action": None, "latency_ms": None,
                              "dropped": False, "apply_frame": None, "done_frame": None})
            inflight[len(decisions) - 1] = pool.submit(ask, sit)
            next_at = now + LIVE["interval_s"]
        return action

    result = live_round(env, fight, on_frame, pace=True)
    for seq in inflight:  # still waiting when the round ended
        decisions[seq]["dropped"] = True
    pool.shutdown(wait=False, cancel_futures=True)
    return result | {"decisions": decisions}


def replay_live_round(env, fight, decisions):
    """Press the logged actions on their apply frames. Check each request's situation against the replayed game."""
    sends = {d["frame"]: d["situation"] for d in decisions}
    applies = {}
    for d in decisions:
        if d["action"] is not None and not d["dropped"]:
            applies.setdefault(d["apply_frame"], []).append(d["action"])

    def on_frame(frame_no, history):
        if frame_no in sends:
            sit = sends.pop(frame_no)
            if not history or situation(history) != sit:
                raise ValueError(f"{fight} frame {frame_no}: logged situation {sit!r} does not match the game")
        return applies.pop(frame_no, [None])[-1]  # two answers on one frame: the newer one wins, as in play

    r = live_round(env, fight, on_frame)
    if sends or applies:
        raise ValueError(f"{fight} frame {min([*sends, *applies])}: the replayed round ended before this frame")
    return r | {"decisions": decisions}


# --- Commands --------------------------------------------------------------------------------------------------------

def open_env():
    """The pinned emulator, or None when no ROM is installed. Exits when the ROM or a save state differs."""
    import stable_retro as retro

    def sha1(path):
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()

    core.INTTYPE = core.integration()
    try:
        rom = retro.data.get_romfile_path(core.GAME, core.INTTYPE)
    except FileNotFoundError:
        return None
    if sha1(rom) != ED["rom_sha1"]:
        raise SystemExit(f"The ROM SHA-1 differs from the edition pin {ED['rom_sha1']}.")
    for f in ED["ladder"]:
        path = retro.data.get_file_path(core.GAME, f["state"] + ".state", core.INTTYPE)
        if not path or sha1(path) != f["sha1"]:
            raise SystemExit(f"Save state {f['state']} is missing or differs from the edition pin.")
    return core.make_env(LADDER[0])


def submission(policy, track, smoke, **fields):
    sub = {"edition": EDITION, "track": track, "model": policy.name, "model_version": policy.version,
           "temperature": ED["temperature"], "rom_sha1": ED["rom_sha1"]} | fields
    SUBMISSIONS.mkdir(exist_ok=True)
    path = SUBMISSIONS / f"{row_id(sub, smoke)}.json"
    path.write_text(json.dumps(sub, indent=1) + "\n")
    print(f"Wrote {path.relative_to(HERE)}. Commit only this file on a branch and open a pull request.")


def cmd_table(policy, smoke):
    tables = {f: policy.distributions(f, state_line(f), SITUATIONS, MOVES) for f in LADDER[: 1 if smoke else None]}
    check_tables(tables)
    submission(policy, "policy_table", smoke, tables=tables)


def cmd_live(policy, smoke):
    env = open_env()
    if env is None:
        raise SystemExit("No ROM installed. Run: uv run mk2_clef.py --rom <your file>")
    rounds = []
    for repeat in range(1 if smoke else ED["repeats"]):
        for fi, fight in enumerate(LADDER[: 1 if smoke else None]):
            r = play_live(env, policy, fight, seeded(repeat, fi))
            rounds.append({"fight": fight, "repeat": repeat} | r)
            print(f"{fight} repeat {repeat}: dealt {r['damage_dealt']} taken {r['damage_taken']} "
                  f"at {r['fps']} fps (self-reported, replay rescores it)")
    submission(policy, "live", smoke, interval_s=LIVE["interval_s"], max_inflight=LIVE["max_inflight"], rounds=rounds)


def replay(env, target, smoke=False):
    """Score a submission file, or the script / random floor rows. Writes results/verified/<id>.jsonl.
    A second replay must match the first: a different result exits and keeps the old file."""
    if target in ("script", "random"):
        sub = {"edition": EDITION, "track": "policy_table", "model": target, "model_version": "harness"}
        policy_fn, official = {"script": script_policy, "random": random_policy}[target], not smoke
    else:
        sub = json.loads(Path(target).read_text())
        if (sub.get("edition"), sub.get("temperature"), sub.get("rom_sha1")) != (EDITION, 1, ED["rom_sha1"]):
            raise SystemExit(f"{target}: edition, temperature or rom_sha1 does not match {EDITION}.")
        try:
            if sub.get("track") == "policy_table":
                official = check_tables(sub["tables"])
                policy_fn = table_policy(sub["tables"])
            elif sub.get("track") == "live":
                official = check_live(sub)
            else:
                raise ValueError("track must be policy_table or live")
        except (ValueError, KeyError, TypeError) as e:
            raise SystemExit(f"{target}: rejected. {e}")
    rid = row_id(sub, not official)
    keep = {k: sub[k] for k in ("edition", "track", "model", "model_version")} | {"official": official}
    rows = []
    if sub["track"] == "policy_table":
        for repeat in range(ED["repeats"] if official else 1):
            for fi, fight in enumerate(LADDER if official else LADDER[:1]):
                r = table_round(env, fight, policy_fn, seeded(repeat, fi))
                rows.append(keep | {"fight": fight, "repeat": repeat} | r)
    else:
        for rd in sub["rounds"]:
            try:
                r = replay_live_round(env, rd["fight"], rd["decisions"])
            except ValueError as e:
                raise SystemExit(f"{target}: rejected. {e}")
            rows.append(keep | {"fight": rd["fight"], "repeat": rd["repeat"]} | r | {"fps": rd["fps"]})  # self-reported
    text = "".join(json.dumps(r) + "\n" for r in rows)
    out = VERIFIED / f"{rid}.jsonl"
    if out.exists() and out.read_text() != text:
        raise SystemExit(f"{out.relative_to(HERE)} exists and this replay differs. Kept the old file. Not deterministic?")
    VERIFIED.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"{rid}: {len(rows)} rounds, mean damage dealt {statistics.mean(r['damage_dealt'] for r in rows):.1f}. "
          f"Wrote {out.relative_to(HERE)}")
    write_board()


def board_row(rid, rows):
    dealt = [r["damage_dealt"] for r in rows]
    n, first = len(rows), rows[0]
    row = {"id": rid, "model": first["model"], "model_version": first["model_version"], "official": first["official"],
           "status": "replay-verified", "mean_dealt": round(statistics.mean(dealt), 1),
           "ci95": round(1.96 * statistics.stdev(dealt) / n ** 0.5, 1) if n > 1 else None,
           "wins": sum(r["won"] for r in rows), "rounds": n,
           "mean_taken": round(statistics.mean(r["damage_taken"] for r in rows), 1),
           "errors": sum("error" in d for r in rows for d in r["decisions"]),
           "coverage": f"{n}/{len(LADDER) * ED['repeats']}"}
    if first["track"] == "live":
        lat = [d["latency_ms"] for r in rows for d in r["decisions"] if not d["dropped"] and d["latency_ms"] is not None]
        row |= {"p50_ms_self_reported": round(statistics.median(lat)) if lat else None,
                "p95_ms_self_reported": round(statistics.quantiles(lat, n=20)[18]) if len(lat) > 1 else None}
    return row


def write_board():
    board = {"edition": EDITION, "prompt_sha256": PROMPT_SHA256, "policy_table": [], "live": []}
    for path in sorted(VERIFIED.glob("*.jsonl")):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        board[rows[0]["track"]].append(board_row(path.stem, rows))
    for track in ("policy_table", "live"):
        board[track].sort(key=lambda r: (not r["official"], -r["mean_dealt"]))
    BOARD.parent.mkdir(exist_ok=True)
    BOARD.write_text(json.dumps(board, indent=1) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("smoke", help="script and random on fight 0, repeat 0, twice. Skips without a ROM.")
    for name in ("table", "live"):
        s = sub.add_parser(name, help=f"run a model, write submissions/<model>--<version>__{name}__{EDITION}.json")
        s.add_argument("--policy", required=True, help="'clef' or path/to/file.py:ClassName (a Policy)")
        s.add_argument("--smoke", action="store_true", help="fight 0, repeat 0 only")
    s = sub.add_parser("replay", help="score a submission file, or 'script' / 'random'")
    s.add_argument("target")
    s.add_argument("--smoke", action="store_true", help="script / random only: fight 0, repeat 0")
    args = p.parse_args()
    if args.cmd == "table":
        return cmd_table(load_policy(args.policy), args.smoke)
    if args.cmd == "live":
        return cmd_live(load_policy(args.policy, live=True), args.smoke)
    env = open_env()
    if env is None:
        if args.cmd == "smoke":
            return print("No ROM installed. Smoke skipped. Install one: uv run mk2_clef.py --rom <your file>")
        raise SystemExit("No ROM installed. Run: uv run mk2_clef.py --rom <your file>")
    if args.cmd == "smoke":
        for target in ("script", "random", "script", "random"):  # the second pass must match the first
            replay(env, target, smoke=True)
        return print("Smoke OK: both replays matched.")
    replay(env, args.target, args.smoke)


if __name__ == "__main__":
    main()
