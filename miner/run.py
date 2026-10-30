"""Run a generation function over pending prompts and submit results to the Pod.

Adapted from 404-Repo/404-base-miner-gpt (MIT) to use only the runtime's packages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import cast

import httpx
from bittensor_wallet import Wallet
from witness_client import AsyncWitnessClient, WitnessConfig

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("miner.run")


async def read_image(source: str | bytes, http: httpx.AsyncClient) -> bytes:
    if isinstance(source, bytes):
        return source
    if source.startswith(("https://", "http://")):
        response = await http.get(source)
        response.raise_for_status()
        return response.content
    return await asyncio.to_thread(Path(source).expanduser().read_bytes)


async def run(
    generate: Callable[[bytes, int, AsyncWitnessClient], Awaitable[str | None]],
    prompts: Mapping[str, str | bytes],
    seed: int,
    *,
    workers: int = 8,
    parallel_calls: int = 3,
    attempts: int = 3,
    wallet_name: str = "miner",
    wallet_hotkey: str = "default",
    wallet_path: str = "~/.bittensor/wallets",
    api: bool = False,
) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
    remaining = dict(prompts)
    if not remaining:
        return
    config = cast(WitnessConfig, json.loads((ROOT / "witness.json").read_bytes()))
    hotkey = Wallet(name=wallet_name, hotkey=wallet_hotkey, path=str(Path(wallet_path).expanduser())).hotkey

    async def try_run() -> None:
        async with (
            AsyncWitnessClient(
                config,
                hotkey=hotkey,
                openai_api_key=os.environ["OPENAI_API_KEY"] if api else None,
                subscription=None if api else "codex",
                parallel_calls=parallel_calls,
            ) as witness,
            httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as http,
        ):
            submitted = await witness.get_submitted()
            queue = asyncio.Queue[tuple[str, str | bytes]]()
            for prompt_id, source in remaining.copy().items():
                if prompt_id in submitted:
                    log.info("%s | already submitted", prompt_id[:12])
                    del remaining[prompt_id]
                else:
                    queue.put_nowait((prompt_id, source))

            async def worker() -> None:
                while not queue.empty():
                    prompt_id, source = queue.get_nowait()
                    started = time.monotonic()
                    try:
                        image = await read_image(source, http)
                        log.info("%s | generating", prompt_id[:12])
                        result = await generate(image, seed, witness)
                        await witness.submit(image, result)
                        log.info(
                            "%s | submitted %s in %.0fs (%d left)",
                            prompt_id[:12],
                            "None" if result is None else f"{len(result)} chars",
                            time.monotonic() - started,
                            len(remaining) - 1,
                        )
                        del remaining[prompt_id]
                    except Exception:  # noqa: BLE001
                        log.exception("Prompt %s failed", prompt_id)

            await asyncio.gather(*(worker() for _ in range(workers)))

    for attempt in range(1, attempts + 1):
        log.info("Attempt %d/%d | %d prompts remaining", attempt, attempts, len(remaining))
        try:
            await try_run()
        except Exception:  # noqa: BLE001
            log.exception("Attempt %d/%d failed", attempt, attempts)
        if not remaining:
            return

    raise RuntimeError(f"{len(remaining)} prompts remain after {attempts} attempts")
