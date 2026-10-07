<p align="center"><img src="site/banner.png" alt="FightBench" width="100%"></p>

A replay-verified benchmark for AI decision models. First game: Mortal Kombat II (Genesis), Liu Kang vs the CPU, played from game memory, not pixels.

**Board:** https://fightbench.aipdv.com

**Setting up with a coding agent?** Point it at [`AGENTS.md`](AGENTS.md).

## Edition `mk2-liukang-v1`

A frozen bench on this emulator: Liu Kang vs the 15 VeryHard CPU fights, 10 repeats each. Rank is mean damage dealt. Wins are reported, not ranked.

- **Edition:** `edition/mk2-liukang-v1.json` holds the 8 situations, 17 moves, ladder, ROM and state SHA-1s, and seed rule. It is the `clef2` prompt, word for word. A changed string is a new edition.
- **Policy Table (headline):** the model answers all 8 situations once per fight, before play. The harness samples at temperature 1, seed `mk2-liukang-v1|repeat|fight`, every 15 frames.
- **Live:** the model answers the current situation every 0.12 s, at most 3 in flight. Replay presses the logged actions and checks each logged situation against the game. Latency is self-reported.
- **Floor rows:** `script` (rules bot) and `random` (17 moves).

```sh
uv run fightbench.py smoke                                 # free, needs the ROM, skips without it
uv run fightbench.py table --policy my.py:MyPolicy         # write submissions/<model>--<version>__policy_table__mk2-liukang-v1.json
uv run fightbench.py live --policy my.py:MyPolicy          # write submissions/<model>--<version>__live__mk2-liukang-v1.json
uv run fightbench.py replay submissions/<file>.json        # maintainer: writes results/verified/ and site/board.json
uv run fightbench.py replay script                         # floor row, full ladder
```

`--policy clef` is Clef-flash. `table --policy clef` reads `ab/clef-cache.json` only and never calls Workers AI. Submit by committing only your `submissions/` file in a pull request. Replay checks the game outcome. It does not re-call the model, so the model name is attested by the submitter. The board is `site/index.html`. A GitHub Action publishes it to https://fightbench.aipdv.com on every merge to `main` that changes `site/`.

## How it started: Clef-flash plays MK2

Clef-flash (Cloudflare Workers AI) plays MK2 on the Sega Genesis. It reads game memory, not pixels.

![Clef-flash beats the VeryHard Shang Tsung with 12 health left](recording/veryhard-win-shang-tsung-2.webp)

Clef-flash beats the VeryHard CPU, replayed frame for frame from the sim. Clef answered 8 situation questions in one call before the round (6,008 tokens, $0.00054). Code reads game memory and plays a move from Clef's probabilities. Full quality: [`recording/veryhard-win-shang-tsung-2.mp4`](recording/veryhard-win-shang-tsung-2.mp4).

Real or luck? (Pre-edition demo, not a FightBench score.) 200 more rounds against the same VeryHard Shang Tsung, prompt `clef4`, fresh seeds:

| Arm | Wins | Damage dealt (of 120) | Rounds with 90+ damage | Closest loss |
|---|---|---|---|---|
| Clef v4 | 1 | **55.0 ± 3.1** | **13** | 4 health short |
| Rules bot | 1 | 43.2 ± 2.8 | 7 | 12 short |
| Random | 0 | 10.0 ± 2.1 | 0 | 37 short |

Wins are rare (about 1 in 200), and on this one fight with the `clef4` prompt Clef out-damaged the rules bot. The frozen FightBench ladder is the fair test: see the board. The face in the corner is MK2's "Toasty!" Easter egg.

Other recordings: `recording/demo.mp4` (2 min), `recording/highlights.jpg`, `recording/calibration-moves.png`.

## How it works

1. stable-retro runs MK2 at 60 fps and reads memory every frame: health, x position, vertical speed, rounds won.
2. Every 0.12 s, the code turns memory into a small JSON state: distance, enemy side, enemy moving, airborne, and `attacks_in_reach` (measured reach per move).
3. Clef-flash answers one `choice` question with a probability for each of 13 moves. Up to 3 requests are in flight. Late answers are dropped.
4. The code samples a move (temperature 0.3) and presses the buttons.
5. The browser shows the game, the move probabilities, and the cost.

Memory map and save states come from stable-retro's `MortalKombatII-Genesis-v0` integration.

## Setup

1. Put your Mortal Kombat II (Genesis) ROM, zip or bin, in `roms/`.
2. Install it. This checks the ROM header and checksum, then builds `integration/` with stable-retro's memory map and save states:
   ```sh
   uv run mk2_clef.py --rom "roms/Mortal Kombat II (W).zip"
   ```
   Your ROM does not need stable-retro's exact SHA-1. The `(W)` dump has a different SHA-1 and works.
3. `.env` holds `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` (Workers AI only). See `.env.example`.

## Run

```sh
uv run mk2_clef.py              # opens http://localhost:8000, press Play
uv run mk2_clef.py --calibrate  # saves calibration/moves.png, one frame per move
```

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `clef-flash` | or `clef` (bigger, slower, 2.7x the price) |
| `--state` | cycle all, easiest first | one fight, for example `Level1.LiuKangVsJax` |
| `--temperature` | `0.3` | 0 = always the top move, 1 = Clef's raw distribution |
| `--interval` | `0.12` | seconds between requests |
| `--max-inflight` | `3` | requests in flight at once |
| `--frame` | off | also send the game frame (about 75 more tokens per request) |
| `--variant` | `baseline` | `tactics` or `random` (see A/B test) |
| `--ab N` | off | headless A/B test, see below |
| `--fights N` | 14 | A/B: only the first N ladder fights (saves quota) |
| `--arms` | all variants | comma list of variants to run in A/B, e.g. `tactics,v2,random` |
| `--sim` | off | headless unpaced experiment, e.g. `random@0,script@24,clef2@0` |
| `--repeats` | `3` | sim: rounds per fight per arm |
| `--ladder` | `VeryEasy.LiuKang` | sim: save-state prefix; `LiuKangVs` = the 15 VeryHard fights (held out) |

## A/B test

```sh
uv run mk2_clef.py --ab 1
```

Plays each of the 14 VeryEasy Liu Kang fights (Rayden to Shao Kahn) once per variant, back to back, with the order flipped every fight. Writes one JSON line per round to `ab/results-*.jsonl` and prints a summary (wins, damage dealt and taken, neurons).

| Variant | What Clef gets |
|---|---|
| `baseline` | 13 moves, distance, reach, health, enemy movement |
| `tactics` | baseline + duck, high and low fireball, flying kick; "got hit / hit enemy in the last 0.75 s"; rules: no long jump-ins, fireball or duck at range, block after a hit. A code guard drops `jump_in` when nothing reaches. |
| `v2` | same 17 moves as tactics with "Use when" descriptions matching state words; state is plain-English lines; adds `threat` question; code reflex blocks instantly when enemy attacks close; threat override when threat > 0.7 and close. |
| `random` | control: uniform random moves at Clef's pace (0.4 s delay), no API calls |

**Sim mode** (`--sim`) runs the same 14 fights headlessly with code-only arms (no Clef, no credentials, unpaced). Arms are `random`, `script`, or either with `+reflex`; append `@N` to set latency in frames (default 24 ≈ Clef's 400 ms). Results go to `ab/sim-*.jsonl`. Useful for H1 (does reflex help?), H2 (does lag hurt?), H3 (does a script beat random?).

**Clef table arms** (`clef`, `clef2`, `clef3`, `clef4`, prompts in `TABLE_PROMPTS`). Clef answers one `choice` question per situation (enemy jumps, enemy attacks close, 60 to 80 px, and so on) for each opponent. Code only detects the situation each frame and samples Clef's answer for it. Answers are cached in `ab/clef-cache.json`, so play has no model delay and repeats are free. A new prompt costs about 500 neurons for 14 opponents. Use `--temperature 1` (Clef's raw distribution): it beat 0 and 0.3, because the CPU punishes predictable play.

Pre-edition experiments, not FightBench scores (2026-10-05, 10 repeats, different seeds and sampler setup):

| Arm | VeryEasy wins / 140 | dealt | VeryHard (held out) wins / 150 | dealt |
|---|---|---|---|---|
| random | 0 | 19.6 | 0 | 8.7 |
| script (rules bot) | 8 | 47.4 | 2 | 33.6 |
| clef2 (reach in px, distance bands) | **20** | **54.9** | 0 | 28.4 |
| clef4 (clef2 + matching "Use when") | 13 | 52.1 | 1 | **35.5** |

How Clef behaves: an open "what now?" question gets a near-flat answer, but a question that names the situation gets a sharp one. Counts in the state ("hit 0 of 4") barely change its answer. It matches words between the situation and the option text, so a conditional clause ("best from more than 100 px") can raise a move's score in the wrong situation.

One run costs about 8,000 neurons (28 Clef rounds), so it needs a fresh free day or the Workers Paid plan. Rounds with Clef errors (for example, quota used up) are flagged in the summary.

## Verified facts (2026-10-02, `(W)` ROM)

- **Pad:** stable-retro emulates a 3-button pad. X, Y and Z do nothing. A = low punch, toward + A = high punch, B = low kick, C = high kick, START = block. Uppercut needs DOWN held first.
- **Memory:** health is 0 to 120. `y_position` is vertical speed, not height.
- **Reach** against an idle Jax: punches and uppercut 70 px, roundhouse 75, high kick 79, low kick 90, sweep 94. Bodies stop at about 45 px.
- **Surprise:** at point-blank range, toward + A is a throw. For Liu Kang, walking in and then pressing toward + A fires his fireball.
- **VeryHard save states start mid-fight.** Level1 and VeryEasy states start with the intro. Clef decides from frame 0.
- **Liu Kang specials** (forward, forward + button): high fireball 17 damage, low fireball 17, flying kick 20, from 100 and 148 px.

## Results so far

- 3 to 5 decisions per second, median latency about 400 ms from India, 0 errors.
- **Clef-flash lost every round** (0 of 13 in two runs). It lands hits, but the CPU combos faster than a 400 ms loop can react.
- Special moves are now in the `tactics` variant. A/B results go in `ab/`.
- Next lever after the A/B test: a code reflex that blocks when the enemy attacks (needs an "enemy attacking" memory address).

## Cost

About 530 input tokens per decision, about 14 neurons per second. The free plan (10,000 neurons per day) is about 12 minutes of play. The cost panel counts this session only.

## Test

```sh
python3 test_mk2_clef.py
```
