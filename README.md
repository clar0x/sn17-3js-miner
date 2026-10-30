# sn17-3js-miner

Image-to-Three.js miner for 404-GEN (Bittensor SN17) Competition 3. `main.py` exposes the async entry point
`generate(image, seed, witness)`; every model call and preview goes through the supplied witness client.

## Pipeline

1. **Four independent render-refine chains** run in parallel. Each drafts a compact module with `gpt-6.1-sol` (a
   different emphasis per chain), then twice renders its latest version on the judge's first-stage views (30°
   left/right, 15° below, 30° above, white background, neutral light) plus its right, back, left and top views,
   and asks `gpt-6.1-sol` at high reasoning effort to fix the largest visible differences and write the full,
   detailed module.
2. **Choice**: `gpt-6-astra` scores every refined version from every chain on the judge's views with the judge's
   0-10 penalty scale, twice with independent candidate orders. `miner/selection.py` combines the mean penalty
   (weight 2) with how much surface detail each render shows (weight 1), both standardized across the
   candidates.

Why a choice among independent chains: in the official round-0 duels between four generations of one agent, the
judge's verdicts were almost perfectly transitive, while the generations themselves varied widely. Picking well
among several independent attempts is therefore the largest available gain. On those duels the panel alone
agreed with the judge on 68% of decided pairs; the combined score agreed on 71% and, as the pick among four
generations, beat the other three by a +32% margin (+38% on the half not used to choose the statistic).

Every module is wrapped to fit the unit cube at 0.99, tip objects that lie flat 15° toward the camera, keep
metalness at or below 0.8 (the judge's gray grid has a black environment map), replace transmission with plain
transparency on scenes heavy enough for it to risk the renderer's time limit, and render opaque surfaces
double-sided. A validator rejection gets one repair per chain with the exact errors, and a chain without a valid
version drafts again; if nothing renders, a lathe built from the photo's measured silhouette and colors
(NumPy/Pillow, integer-only) is returned. Renders are paced through a small shared gate so the Pod's single-job
renderer queue never overflows.

## Budget, tiers and replay

Every call owns a fixed credit allowance (4.68M in total over at most 18 calls); its output cap follows from that
allowance and an upper estimate of its input at frozen rates, so the prompt stays inside the 5M-credit / 60-call
budget and no decision depends on which call finishes first.

Service tiers are chosen per call type: drafts use Fast, refinements Ultrafast and panel calls Standard. The Pod
runs two model calls at a time, and the refinements carry most of the output, so this is what lets 128 prompts
finish inside the 24-hour generation window while using about half of a Pro subscription's weekly allowance. A
chain whose account refuses a faster tier continues at Standard, and on such an account the third and fourth
chains stop refining, so a Standard-only rerun does about the work of two chains.

Request bodies depend only on the image, the seed and recorded responses; candidate order for the panel is
shuffled with `random.Random(f"{seed}:{sha256(image)}:{request_id}")`, and the render statistics are computed
from the recorded render bytes with exact integer sums. Provider errors drop the affected candidate; connection
and infrastructure errors propagate to the runner.

## Running

Use the 404 runtime (Python 3.13 with the pinned packages in `pyproject.toml` plus the witness and renderer
clients) and put the proof parameters in `proof-parameters/`.

```bash
python main.py --prompts prompts.txt --seed <round seed> --wallet-name <wallet> --wallet-hotkey <hotkey> \
    --parallel-prompts 8 --parallel-calls 3            # Codex subscription (default)
OPENAI_API_KEY=... python main.py ... --api           # or an OpenAI API key
```

Prompts already submitted to the Pod are skipped, so the command can be rerun after an interruption.

## License

MIT. The batch runner and entry point are adapted from `404-Repo/404-base-miner-gpt` (MIT).
