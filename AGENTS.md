# Agent setup guide

Instructions for a coding agent that sets up this repo for a user. Read `README.md` for what the project does.

## Rules

- **Never download a ROM.** The user must supply their own Mortal Kombat II (Genesis) ROM. Ask for the file path.
- Never print, log, or commit `CLOUDFLARE_API_TOKEN`. Never commit `.env`, `roms/`, `integration/`, or `ab/clef-cache.json`.
- Ask before you spend Workers AI quota. Steps 1 to 4 below are free.

## Steps

1. Install `uv` if `uv --version` fails. Ask the user first: https://docs.astral.sh/uv/getting-started/installation/
2. Run the checks. They need no ROM and no credentials:
   ```sh
   python3 test_mk2_clef.py   # prints "ok"
   ```
3. Install the ROM (zip or bin). This checks the header and checksum, then builds `integration/`:
   ```sh
   uv run mk2_clef.py --rom "<path to the user's ROM>"
   ```
   Errors: "not a Sega Genesis Mortal Kombat II ROM" means the wrong game or `.smd` format. "checksum is wrong" means a damaged dump.
4. Confirm the emulator works. This is free, takes a few seconds, and needs no credentials:
   ```sh
   uv run mk2_clef.py --sim random@0,script@0 --fights 2 --repeats 1
   ```
   Expect a summary table with `random@0` and `script@0` rows.
5. Credentials. Copy `.env.example` to `.env` and ask the user for:
   - `CLOUDFLARE_ACCOUNT_ID`: Cloudflare dashboard, then Workers AI.
   - `CLOUDFLARE_API_TOKEN`: a token with the Workers AI permission only.
6. Play. This opens http://localhost:8000. The user presses Play:
   ```sh
   uv run mk2_clef.py
   ```
   Headless: `BROWSER=true uv run mk2_clef.py`.

## Cost

- The free plan gives 10,000 neurons per day. Live play uses about 14 neurons per second.
- A Clef table run (`--sim clef2@0 --temperature 1`) costs about 500 neurons the first time. Answers are cached in `ab/clef-cache.json`, so repeats are free.

## Gotchas

- `index.html` is read once at server start. Restart after you edit it.
- Stop a long run by its PID. `pkill -f mk2_clef.py` also kills other shells whose command line contains that text.
- Without `--state`, the game cycles through all save states. `VeryHard` states start mid-fight.
