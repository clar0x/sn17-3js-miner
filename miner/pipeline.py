"""Independent render-refine chains, then a judge-calibrated choice among all their versions.

Four chains run in parallel. Each drafts a compact module with Sol from the photo, then twice renders its latest
version on the judge's first-stage views and asks Sol at high effort to fix the largest visible differences and
add the detail. Every valid version from every chain is then scored by two
shuffled Astra panels on those views with the judge's penalty scale, and selection.choose combines
the mean penalty with how detailed each render is: the signals that best predicted the official judge
between independent generations of one agent.

A rejected module gets one Sol repair per chain with the validator's errors, and a chain without a
valid version spends its next step on a fresh draft. If nothing renders, a lathe built from the
measured silhouette is returned instead of nothing. Provider errors only drop the affected call's
result; connection and infrastructure errors propagate to the runner for recovery. Every call has a
fixed share of the prompt budget, so no decision depends on which call finishes first.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import weakref
from dataclasses import dataclass
from typing import Any, cast

from openai import APIConnectionError, APIError, BadRequestError
from openai.types.responses import Response, ResponseInputParam
from witness_client import AsyncWitnessClient, RenderOptions, RenderRejected, WitnessError

from . import analysis, fallback, prompts, selection
from .budget import Budget, estimate_input, output_cap
from .wrap import extract, wrap

log = logging.getLogger("miner.pipeline")

ASTRA = "gpt-6-astra"
SOL = "gpt-6.1-sol"
# Service tiers per call type. All are allowed and counted at Standard rates; they differ in speed and in the
# subscription share they use (Fast 2.5x, Ultrafast 8x). The Pod runs two model calls at a time, so a round of
# 128 prompts fits in its 24 hours only with the refinements, the bulk of the output, at Ultrafast.
DRAFT_TIER = "priority"  # Fast
REFINE_TIER = "ultrafast"
PANEL_TIER = "default"  # short outputs
TIER_RETRY_OFFSET = 1000  # request_id offset for a call resent at the default tier

# Stage 1 of the duel judge compares exactly these four views, rendered on white with neutral lighting.
JUDGE_VIEWS = RenderOptions(
    thetas=(30, 330, 0, 0), phis=(0, 0, 15, -30), img_size=512, bg_color="ffffff", lighting="neutral"
)
# Right, back, left and top: the judge's side guard looks for missing or paper-thin geometry there.
AROUND_VIEWS = RenderOptions(
    thetas=(90, 180, 270, 0), phis=(0, 0, 0, -90), img_size=384, bg_color="808080", lighting="neutral"
)

# Per chain: draft model, draft reasoning effort, index into prompts.DRAFT_FOCUS. Drafts are only starting
# points for two high-effort refinements; medium effort keeps a 128-prompt round inside the subscription.
CHAINS = ((SOL, "medium", 0), (SOL, "medium", 1), (SOL, "medium", 2), (SOL, "medium", 3))
STEPS = 2
# A chain whose account refuses a faster tier runs about three times slower, so beyond these chains it stops
# refining: an operator rerunning this code without the faster tiers does about the work of two chains.
STANDARD_TIER_CHAINS = 2
REFINE_MODEL, REFINE_EFFORT = SOL, "high"
REPAIR_MODEL, REPAIR_EFFORT = SOL, "low"
PANEL_MODEL, PANEL_EFFORT = ASTRA, "low"
PANEL_IDS = (100, 101)  # two independently shuffled scorings, averaged

# Credit allowance per call. Worst case: 4 x 0.3M drafts, 8 x 0.25M refinements, 4 x 0.17M repairs
# and 2 x 0.4M panels = 4.68M of the 5M prompt budget (input estimates run about 1.5x high), in 18 of
# the 60 allowed calls.
DRAFT_ALLOWANCE = {ASTRA: 850_000, SOL: 300_000}
REFINE_ALLOWANCE = 250_000
REPAIR_ALLOWANCE = 170_000
PANEL_ALLOWANCE = 400_000
CODE_TOKENS = 32_768
PANEL_TOKENS = 8_000

# The renderer runs one job at a time and refuses requests beyond a queue of 8 or a 120 s wait, so all
# prompts in one process share a small gate. It only paces requests; their content is unaffected.
RENDER_CONCURRENCY = 2
_render_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


@dataclass
class Lane:
    """Whether the account has refused a faster tier in this chain; from then on its calls use the standard one.

    Each chain keeps its own, so a chain's requests depend only on its own recorded responses.
    """

    refused: bool = False

    def tier(self, preferred: str) -> str:
        return "default" if self.refused else preferred


@dataclass(frozen=True)
class Candidate:
    name: str
    raw: str  # module text as the model wrote it
    source: str  # wrapped module that is rendered and returned
    views: bytes  # JUDGE_VIEWS grid
    look: selection.Look | None  # statistics of that grid


def png_data_url(image: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(image).decode("ascii")


def text_item(text: str) -> dict[str, str]:
    return {"type": "input_text", "text": text}


def image_item(image: bytes) -> dict[str, str]:
    return {"type": "input_image", "image_url": png_data_url(image), "detail": "high"}


def input_estimate(instructions: str, content: list[dict[str, str]]) -> int:
    texts = len(instructions) + sum(len(item["text"]) for item in content if item["type"] == "input_text")
    images = sum(1 for item in content if item["type"] == "input_image")
    return estimate_input(texts, images)


class Session:
    def __init__(self, image: bytes, seed: int, witness: AsyncWitnessClient) -> None:
        self.image, self.seed, self.witness = image, seed, witness
        self.digest = hashlib.sha256(image).hexdigest()
        self.facts = analysis.measure(image)  # only for the fallback lathe
        self.budget = Budget()

    # -- model calls ---------------------------------------------------------------------------

    async def call(
        self, request_id: int, model: str, max_output: int, lane: Lane, tier: str, **params: Any
    ) -> Response | None:
        tier = lane.tier(tier)
        try:
            result = await self.witness.responses.create(
                request_id=request_id,
                model=model,
                store=False,
                service_tier=tier,
                max_output_tokens=max_output,
                **params,
            )
        except APIConnectionError:
            raise
        except (WitnessError, APIError) as error:
            if isinstance(error, WitnessError) and error.source != "provider":
                raise
            self.budget.failed()
            if tier != "default" and isinstance(error, BadRequestError) and "service_tier" in str(error.body):
                # This account lacks the faster tier: the rest of this chain uses the standard one.
                log.warning("%s call %d: %s unsupported, using default", self.digest[:12], request_id, tier)
                lane.refused = True
                return await self.call(request_id + TIER_RETRY_OFFSET, model, max_output, lane, "default", **params)
            log.warning("%s call %d (%s) failed: %s", self.digest[:12], request_id, model, error)
            return None
        self.budget.charge(model, result.usage)
        return result

    async def write(
        self,
        request_id: int,
        model: str,
        effort: str,
        allowance: int,
        content: list[dict[str, str]],
        lane: Lane,
        tier: str,
    ) -> str | None:
        cap = output_cap(model, input_estimate(prompts.GENERATE, content), allowance, CODE_TOKENS)
        if cap <= 0:
            return None
        result = await self.call(
            request_id,
            model,
            cap,
            lane,
            tier,
            reasoning={"effort": effort},
            instructions=prompts.GENERATE,
            input=cast(ResponseInputParam, [{"role": "user", "content": content}]),
        )
        if result is None or result.status != "completed":
            return None
        return extract(result.output_text) or None

    async def render(self, source: str, options: RenderOptions) -> bytes:
        loop = asyncio.get_running_loop()
        gate = _render_gates.get(loop)
        if gate is None:
            gate = _render_gates[loop] = asyncio.Semaphore(RENDER_CONCURRENCY)
        async with gate:
            return await self.witness.render(source, options)

    # -- candidates ----------------------------------------------------------------------------

    async def materialize(self, name: str, raw: str) -> Candidate | str:
        """Render a module on the judge views, or return the validator's rejection."""
        source = wrap(raw)
        if source is None:
            return "The module must have exactly one default export: function generate(THREE)."
        try:
            views = await self.render(source, JUDGE_VIEWS)
        except RenderRejected as rejection:
            return rejection.detail_json
        return Candidate(name, raw, source, views, selection.measure(views, JUDGE_VIEWS.img_size, JUDGE_VIEWS.gap))

    async def produce(
        self,
        name: str,
        request_id: int,
        model: str,
        effort: str,
        allowance: int,
        content: list[dict[str, str]],
        repairs: list[int],
        lane: Lane,
        tier: str,
    ) -> Candidate | None:
        """One model call plus, while the chain still has its repair, one repair of a rejected module."""
        raw = await self.write(request_id, model, effort, allowance, content, lane, tier)
        if raw is None:
            return None
        outcome = await self.materialize(name, raw)
        if isinstance(outcome, Candidate):
            return outcome
        if not repairs:
            return None
        repaired = await self.write(
            repairs.pop(), REPAIR_MODEL, REPAIR_EFFORT, REPAIR_ALLOWANCE, self.repair_content(raw, outcome), lane, tier
        )
        if repaired is None:
            return None
        outcome = await self.materialize(name + "r", repaired)
        return outcome if isinstance(outcome, Candidate) else None

    def draft_content(self, focus: int) -> list[dict[str, str]]:
        return [text_item(prompts.DRAFT_FOCUS[focus] + " " + prompts.DRAFT_LENGTH), image_item(self.image)]

    def repair_content(self, raw: str, errors: str) -> list[dict[str, str]]:
        return [
            text_item(prompts.REPAIR + "\n\nValidation errors:\n" + errors + "\n\nRejected module:\n" + raw),
            image_item(self.image),
        ]

    def refine_content(self, current: Candidate, around: bytes | None) -> list[dict[str, str]]:
        content = [text_item(prompts.REFINE), image_item(self.image), image_item(current.views)]
        if around is not None:
            content.append(image_item(around))
        content.append(text_item("Current module:\n" + current.raw))
        return content

    async def chain(self, index: int) -> list[Candidate]:
        model, effort, focus = CHAINS[index]
        base = 10 * index
        repairs = [base + 2]
        lane = Lane()
        current = await self.produce(
            f"c{index}d",
            base + 1,
            model,
            effort,
            DRAFT_ALLOWANCE[model],
            self.draft_content(focus),
            repairs,
            lane,
            DRAFT_TIER,
        )
        versions = [] if current is None else [current]
        for step in range(STEPS):
            if index >= STANDARD_TIER_CHAINS and lane.refused:
                break
            name = f"c{index}s{step + 1}"
            if current is None:
                content = self.draft_content(focus)  # no valid version yet: draft again
            else:
                try:
                    around: bytes | None = await self.render(current.source, AROUND_VIEWS)
                except RenderRejected:
                    around = None
                content = self.refine_content(current, around)
            refined = await self.produce(
                name, base + 3 + step, REFINE_MODEL, REFINE_EFFORT, REFINE_ALLOWANCE, content, repairs, lane, REFINE_TIER
            )
            if refined is not None:
                versions.append(refined)
                current = refined
        return versions

    # -- panel ---------------------------------------------------------------------------------

    async def score(self, request_id: int, candidates: list[Candidate]) -> tuple[dict[int, float], int | None] | None:
        """One shuffled panel scoring: penalty per candidate index and the panel's named best index."""
        order = list(range(len(candidates)))
        random.Random(f"{self.seed}:{self.digest}:{request_id}").shuffle(order)  # nosec B311 - seeded order
        labels = [chr(65 + i) for i in range(len(order))]
        content = [text_item("REFERENCE PHOTO:"), image_item(self.image)]
        for label, index in zip(labels, order, strict=True):
            content += [text_item(f"CANDIDATE {label}:"), image_item(candidates[index].views)]
        cap = output_cap(PANEL_MODEL, input_estimate(prompts.PANEL, content), PANEL_ALLOWANCE, PANEL_TOKENS)
        if cap <= 0:
            return None
        result = await self.call(
            request_id,
            PANEL_MODEL,
            cap,
            Lane(),
            PANEL_TIER,
            reasoning={"effort": PANEL_EFFORT},
            instructions=prompts.PANEL,
            input=cast(ResponseInputParam, [{"role": "user", "content": content}]),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "panel",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "candidates": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "enum": labels},
                                        "issues": {"type": "string"},
                                        "penalty": {"type": "number"},
                                    },
                                    "required": ["label", "issues", "penalty"],
                                    "additionalProperties": False,
                                },
                            },
                            "best": {"type": "string", "enum": labels},
                        },
                        "required": ["candidates", "best"],
                        "additionalProperties": False,
                    },
                }
            },
        )
        if result is None or result.status != "completed":
            return None
        try:
            parsed = json.loads(result.output_text)
            penalties: dict[int, float] = {}
            for entry in parsed["candidates"]:
                if entry["label"] in labels:
                    penalties.setdefault(order[labels.index(entry["label"])], float(entry["penalty"]))
            named = order[labels.index(parsed["best"])] if parsed["best"] in labels else None
        except (ValueError, KeyError, TypeError):
            return None
        if len(penalties) != len(candidates):
            return None
        return penalties, named

    async def panel(self, candidates: list[Candidate]) -> Candidate:
        """Average the penalties of independently shuffled scorings, then combine them with the looks."""
        scorings = [s for s in await asyncio.gather(*(self.score(i, candidates) for i in PANEL_IDS)) if s is not None]
        totals = (
            {index: sum(s[0][index] for s in scorings) / len(scorings) for index in range(len(candidates))}
            if scorings
            else None
        )
        best = selection.choose(totals, [c.look for c in candidates])
        log.info(
            "%s panel x%d: %s -> %s",
            self.digest[:12],
            len(scorings),
            ", ".join(
                f"{c.name}={'-' if totals is None else f'{totals[i]:.2f}'}"
                f"/{'-' if c.look is None else f'{c.look.darkness:.0f},{c.look.detail:.1f}'}"
                for i, c in enumerate(candidates)
            ),
            candidates[best].name,
        )
        return candidates[best]

    # -- pipeline ------------------------------------------------------------------------------

    async def run(self) -> str | None:
        chains = await asyncio.gather(*(self.chain(index) for index in range(len(CHAINS))))
        # A chain's draft competes only when none of its refinements rendered: refinements almost always win.
        candidates = [candidate for versions in chains for candidate in (versions[1:] or versions)]
        if not candidates:
            return await self.fallback()
        winner = candidates[0] if len(candidates) == 1 else await self.panel(candidates)
        log.info("%s final %s (%s)", self.digest[:12], winner.name, self.budget.summary())
        return winner.source

    async def fallback(self) -> str | None:
        source = wrap(fallback.module(self.facts))
        if source is None:
            return None
        try:
            await self.render(source, JUDGE_VIEWS)
        except RenderRejected:
            return None
        log.warning("%s no candidate rendered; returning the measured fallback (%s)", self.digest[:12], self.budget.summary())
        return source


async def generate(image: bytes, seed: int, witness: AsyncWitnessClient) -> str | None:
    return await Session(image, seed, witness).run()
