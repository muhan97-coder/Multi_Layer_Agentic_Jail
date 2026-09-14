# Third-party notices

## Required dependencies: none

`pyproject.toml` declares `dependencies = []`. The supported free-tier path (`mlaj` / `python -m agi_v8_1.runtime.free_tier_cli`, T0–T2, and the cold-start check) was verified in a virtual environment with no third-party packages installed. It uses only the Python standard library.

## Optional imports present in the source

Walking every `import` statement in the 138 shipped Python sources finds three third-party names. They live in modules that are shipped for completeness but are **outside** the supported free-tier path; nothing in T0–T2 imports them, and the CLI never triggers them.

| Package | Where it is imported | Why it is not needed here | License |
|---|---|---|---|
| pydantic | `agi_v8_1/agent_system/swarm_v8/config.py`, `router_freeze.py`, `schemas.py` (swarm configuration models) | swarm lanes are a T4 capability; no swarm runs in this cut | MIT |
| cryptography | `agi_v8_1/policy/tier_token.py` (Ed25519 verification of tier tokens) | token import is a T3+ path; this cut refuses it before touching the library | Apache-2.0 OR BSD-3-Clause (dual) |
| python-dotenv | `agi_v8_1/runtime/tick_runner.py` (`.env` loading for the tick runner) | the tick runner is never started by the public CLI, which reads no `.env` | BSD-3-Clause |

If you install any of them, their license texts are in the package's own distribution metadata (`<package>-<version>.dist-info/`). Upstream: https://github.com/pydantic/pydantic · https://github.com/pyca/cryptography · https://github.com/theskumar/python-dotenv.

## Not dependencies of this cut

OpenAI, Anthropic, Google, DeepSeek and OpenRouter SDKs or HTTP clients; Docker; pytest; NumPy. None is imported by the shipped sources. If a future cut adds any of them, this file must be updated in the same change.

## Attribution for shipped assets

`agi_v8_1/public_assets/free_tiers/prompts/strategist.md` is an original offline prompt skeleton written for this repository. `agi_v8_1/public_assets/curriculum/local_tiers.json` and `agi_v8_1/public_assets/free_tiers/config.json` are original configuration files. No third-party datasets, benchmark tasks, gold patches, or model outputs are included.
