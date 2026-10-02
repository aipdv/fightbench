# Clef-flash plays Mortal Kombat II

Clef-flash (Cloudflare Workers AI) plays MK2 on the Sega Genesis. It reads game memory, not pixels.

Demo: `recording/demo.mp4` (2 min), `recording/highlights.jpg`, `recording/calibration-moves.png`.

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

## Verified facts (2026-10-02, `(W)` ROM)

- **Pad:** stable-retro emulates a 3-button pad. X, Y and Z do nothing. A = low punch, toward + A = high punch, B = low kick, C = high kick, START = block. Uppercut needs DOWN held first.
- **Memory:** health is 0 to 120. `y_position` is vertical speed, not height.
- **Reach** against an idle Jax: punches and uppercut 70 px, roundhouse 75, high kick 79, low kick 90, sweep 94. Bodies stop at about 45 px.
- **Surprise:** at point-blank range, toward + A is a throw. For Liu Kang, walking in and then pressing toward + A fires his fireball.
- **VeryHard save states start mid-fight.** Level1 and VeryEasy states start with the intro. Clef decides from frame 0.

## Results so far

- 3 to 5 decisions per second, median latency about 400 ms from India, 0 errors.
- **Clef-flash lost every round** (0 of 13 in two runs). It lands hits, but the CPU combos faster than a 400 ms loop can react.
- Next levers: a code reflex that blocks when the enemy attacks (needs an "enemy attacking" memory address), and character special moves.

## Cost

About 530 input tokens per decision, about 14 neurons per second. The free plan (10,000 neurons per day) is about 12 minutes of play. The cost panel counts this session only.

## Test

```sh
python3 test_mk2_clef.py
```
