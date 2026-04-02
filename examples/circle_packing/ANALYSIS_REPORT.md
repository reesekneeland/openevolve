# OpenEvolve: System Analysis & Circle Packing Run Report

## 1. System Overview & Architecture

### What OpenEvolve Does

OpenEvolve is an open-source reimplementation of DeepMind's AlphaEvolve — an evolutionary coding agent that uses LLMs to iteratively mutate and improve programs. Rather than searching a parameter space, it searches *program space*: the LLM proposes code changes, an evaluator scores the result, and a population-based algorithm retains the most promising variants.

### Architecture (End-to-End Data Flow)

A single iteration works like this:

1. **Sampling** — `ProcessParallelController` calls `database.sample_from_island(island_id)`, which selects a parent program via exploration/exploitation/weighted strategy, plus "inspiration" programs from other islands or the archive.

2. **Snapshot** — The database is serialized into a dict and passed to a worker process (true parallelism via `ProcessPoolExecutor`, avoiding the GIL).

3. **Prompt Building** — `PromptSampler.build_prompt()` assembles a system message + user message containing the current program, its metrics, evolution history (prior attempts with diffs), and top-performing programs. Template selection depends on mode (diff-based vs full rewrite).

4. **LLM Generation** — `LLMEnsemble.generate_with_context()` picks a model by weighted random sampling and calls the OpenAI-compatible API. The response is parsed by `parse_full_rewrite()` (extracts code from ` ```python ``` ` blocks) or `extract_diffs()` + `apply_diff()`.

5. **Evaluation** — The extracted code is written to a temp file and evaluated in a **subprocess** (crash isolation). The evaluator runs a cascade: Stage 1 (quick validation) → Stage 2 (full scoring), with configurable pass/fail thresholds at each stage.

6. **Storage** — Results flow back to the main process. `Database.add()` computes feature coordinates, checks novelty, updates the MAP-Elites grid cell for the target island, and tracks the global best.

### Key Design Patterns

| Pattern | Implementation | Purpose |
|---|---|---|
| **Island-based MAP-Elites** | `database.py` — multiple isolated populations with separate feature grids | Maintain diversity, prevent premature convergence |
| **Cascade Evaluation** | `evaluator.py` — progressive stages with thresholds | Filter bad programs cheaply before expensive evaluation |
| **Process Workers** | `process_parallel.py` — `ProcessPoolExecutor` with database snapshots | True parallelism, crash isolation |
| **Double Selection** | Programs for "inspiration" differ from the parent being mutated | Encourage cross-pollination without disrupting lineage |
| **Lazy Migration** | Islands migrate based on generation counts, not wall-clock time | Prevents premature homogenization |

---

## 2. Running the Example: What Matched and What Surprised

We ran the circle packing example across five configurations, progressively improving model quality to understand what drives performance:

| Run | Model | Phase | Best Score | Sum Radii | % Target | Duration |
|-----|-------|-------|-----------|-----------|----------|----------|
| A | gemini-2.5-flash-lite | P1 | 0.6421 | 1.692 | 64.2% | 2.5 min |
| A | gemini-2.5-flash-lite | P2 | 0.8428 | 2.221 | 84.3% | 6 min |
| B | gemini-2.5-flash-lite (scipy avail) | P2 | 0.7168 | 1.889 | 71.7% | 4 min |
| C | Claude Sonnet 4 + Haiku 3.5 | P2 | 0.8692 | 2.290 | 86.9% | 41 min |
| **D** | **Sonnet 4.5 + Opus 4.5** | **P1** | **0.9040** | **2.382** | **90.4%** | **23 min** |
| **D** | **Sonnet 4.5 + Opus 4.5** | **P2** | **0.9686** | **2.552** | **96.9%** | **65 min** |

**What matched expectations:**
- The process pool kept 4 workers saturated across all runs. Island-round-robin scheduling worked as designed.
- Cascade evaluation filtered invalid programs instantly — index-out-of-bounds, undefined variables, and shape mismatches all scored 0.0 at Stage 1 with negligible time cost.
- Checkpoints saved reliably, and Phase 2 seamlessly resumed from Phase 1's best program in every configuration.
- Island differentiation was genuine — some islands converged while others maintained exploration. This held true regardless of model quality.

**What surprised:**

- **Model quality dominated all other factors — decisively.** This was the clearest signal across all runs. With the same config, evaluation, and population management, Sonnet 4.5 + Opus 4.5 (Run D) reached 90.4% of target in Phase 1 alone — exceeding what Gemini achieved after both phases combined (84.3%). The framework itself is largely model-agnostic; the LLM is the binding constraint.

- **The `parse_full_rewrite` fallback is dangerous.** When we first tried `gemini-2.5-flash` (a thinking model), its chain-of-thought prose was treated as Python code because the function falls back to returning raw LLM output when no code block is found (`code_utils.py:120`). 100% of iterations produced SyntaxErrors until we switched to the non-thinking `gemini-2.5-flash-lite`.

- **Failure modes are model-specific and repetitive.** Gemini wasted 30 iterations on `import scipy` (ModuleNotFoundError). Claude Sonnet 4 wasted 20 iterations removing `run_packing()`. These errors repeated because pre-evaluation failures don't feed back into prompts. Sonnet 4.5 + Opus 4.5 largely avoided these categories — only 2 runtime errors in 100 Phase 1 iterations — but traded them for a different problem: 34 timeouts in Phase 2, where the models generated compute-heavy optimization programs that exceeded the 90-second evaluation limit.

- **Making scipy available didn't help without model capability.** Run B (Gemini + scipy installed) scored *worse* than Run A (Gemini, no scipy) — 0.7168 vs 0.8428. The model never even attempted scipy-based code. Environment is necessary but not sufficient; the LLM must be capable of formulating the constrained optimization problem.

- **Stronger models produce fewer errors but more timeouts.** Sonnet 4.5 had a 98% success rate in Phase 1 (vs Gemini's 82%), but Phase 2 saw 34 timeouts — programs running heavy multi-stage optimization that couldn't complete in 90 seconds. The best Phase 2 program took 48.4 seconds. Some of those timed-out programs may have been breakthroughs.

---

## 3. Strengths and Weaknesses

### Strengths

**Process isolation is excellent.** Each evaluation runs in a fresh subprocess. In Run D's Phase 2, programs ran multi-stage numerical optimization for up to 48 seconds — timed-out programs were killed cleanly without affecting other workers. Non-negotiable for untrusted LLM-generated code, and the implementation is solid.

**Island-based MAP-Elites produces genuine diversity at every model quality level.** Run D Phase 1 final status:

| Island | Programs | Best | Avg | Diversity |
|--------|----------|------|-----|-----------|
| 0 | 9 | **0.9040** | 0.8417 | 440.40 |
| 1 | 8 | 0.8449 | 0.8137 | 459.05 |
| 2 | 12 | 0.7946 | 0.7730 | 368.88 |
| 3 | 12 | 0.8557 | 0.8328 | 1160.80 |

All four islands independently discovered strong solutions (0.79–0.90 range) while maintaining distinct algorithmic approaches (diversity 369–1161). Run D Phase 2 showed this even more clearly — all 5 islands had best scores above 0.92, with Island 4 (the best at 0.9686) having the *lowest* diversity (681), while Island 2 (0.9535) had the *highest* (1769). The system balanced convergence and exploration across islands exactly as designed.

**The two-phase evolution strategy works.** Every configuration showed the same pattern: Phase 1 establishes a strong baseline, then Phase 2's reconfigured prompt and population parameters break through the plateau. Run D went from 0.9040 → 0.9686, with 6 best-program events still occurring in the second half of Phase 2 — the system hadn't converged even at iteration 90.

**Checkpoint/resume is robust.** Full database state serialized every N iterations with graceful signal handling. We ran 5 different configurations across multiple sessions without any data loss or corruption.

### Weaknesses

**The prompt → code extraction pipeline is fragile.** `parse_full_rewrite` falls through: (1) ` ```python ``` ` block → (2) any ` ``` ``` ` block → (3) raw text as code. Tier 3 treats any response as code, which means thinking-model output or analysis prose gets executed as Python. This made `gemini-2.5-flash` completely unusable without a code change.

**No feedback loop from pre-evaluation failures.** The most consistent waste across runs. Gemini repeated `import scipy` 30 times. Claude Sonnet 4 repeated the `run_packing` error 20 times. The infrastructure already exists — `previous_programs` supports error messages — but pre-evaluation failures are silently discarded. Sonnet 4.5 mostly avoided this thanks to better instruction-following, but the problem is architectural: it surfaces with every new model that makes a systematic mistake.

**Evaluation timeout is a ceiling for strong models.** Run D's Phase 2 had 34 timeouts at the 90-second limit — more than a third of iterations. These models generate sophisticated multi-stage optimization programs that need real compute time. The Phase 2 config sets `timeout: 90`, but the README's Anthropic config sets `timeout: 600`. Some of those timed-out programs may have been breakthroughs. This is arguably the largest gap between our Phase 2 setup and the README's — we were inadvertently capping the models' best work.

**The `Database` class is a god object.** At 1300+ lines, it handles MAP-Elites grids, island management, program storage, serialization, sampling strategies, novelty checking, feature coordinate calculation, diversity caching, and artifact management — at least 4-5 separate concerns.

**Snapshot staleness in parallel execution.** Each worker gets a database snapshot at submission time. By the time it finishes (3-65+ seconds later), the database has been mutated by other completed iterations. For 4 workers this is tolerable; at higher parallelism the LLM would be shown programs that no longer exist in the population.

---

## 4. Theoretical Framework & Research Directions

### What OpenEvolve Actually Is, Formally

OpenEvolve implements a specific point in the design space of program synthesis algorithms. Formally, it performs **quality-diversity optimization in program space using an LLM as a learned mutation operator**. This combines three distinct research traditions:

1. **MAP-Elites** (Mouret & Clune, 2015) — the quality-diversity algorithm that maintains one solution per behavioral niche, producing a diverse archive rather than a single optimum.
2. **Island-model evolutionary algorithms** — parallel isolated populations with periodic migration, preventing premature convergence.
3. **LLM-guided program mutation** — using a language model's learned prior over code transformations as the variation operator, replacing the syntactically random mutations of traditional genetic programming.

The lineage is direct: FunSearch (DeepMind, Nature 2024) introduced the idea of combining LLM code generation with island-based evolutionary search for mathematical discovery. AlphaEvolve (DeepMind, 2025) extended it to multi-file codebases with an LLM ensemble. OpenEvolve is an open-source reimplementation of the AlphaEvolve architecture.

### Why the LLM-as-Mutation-Operator Framing Matters

The key theoretical insight, formalized by Lehman et al. (2022) in "Evolution through Large Models," is that traditional GP mutations (point mutation, subtree crossover) are *syntactically* valid but *semantically* random — most random code changes produce garbage. An LLM mutation is semantically informed because the model has learned the distribution of *meaningful* code transformations from its training corpus. This is equivalent to replacing a uniform mutation distribution with a highly structured prior.

This explains what we observed across runs: the mutation operator's quality (model capability) dominated all other factors. The framework's selection and diversity mechanisms worked identically across all configurations — what changed was the *quality of the proposals* being selected from. A uniform mutation operator would need astronomically more samples to find the same programs.

But this framing also reveals a fundamental limitation of the current design: **the LLM is treated as a static, memoryless mutation operator.** Each iteration, the LLM sees the current program and some context, generates a mutation, and that's it. The LLM doesn't learn from the evolution process. It doesn't build a model of *why* certain mutations worked. It doesn't form hypotheses about the problem structure. It's a very expensive random number generator with a good prior — but it's still just generating samples, not reasoning about the search landscape.

### What's Missing: Hypothesis-Driven Search

The current prompt template (`FULL_REWRITE_USER_TEMPLATE`) gives the LLM:
- The current program and its metrics
- A history of previous attempts with their scores
- Top-performing programs from the archive
- "Inspiration" programs from other islands

Then it says: *"Rewrite the program to improve its performance."*

This is essentially saying "here's the data, make it better" — the equivalent of asking a scientist to "do better science" without asking them to formulate a hypothesis first. Compare this to how a human expert would approach the circle packing problem:

1. **Observe:** "The current approach uses concentric rings and scores 1.69. Looking at the top programs, hexagonal arrangements score higher."
2. **Hypothesize:** "The concentric ring approach wastes space near the boundary. I hypothesize that placing circles along the edges first, then filling the interior, would improve boundary utilization."
3. **Design experiment:** Write specific code to test that hypothesis.
4. **Analyze results:** "Boundary-first improved to 1.92 but the interior is underutilized. The hypothesis was partially confirmed — boundary matters, but the interior strategy also needs work."
5. **Iterate:** Refine the hypothesis based on what was learned.

OpenEvolve skips steps 1, 2, and 4 entirely. The LLM implicitly does some of this reasoning, but it's never made explicit, never recorded, and never accumulated across iterations. Each iteration starts from scratch conceptually, even though the prompt includes prior results.

### Proposed Research Directions

#### 1. Reflective Evolution (Verbal Gradients)

The most immediately implementable improvement, inspired by ReEvo (Ye et al., NeurIPS 2024). Instead of a single "improve this" prompt, split each iteration into two LLM calls:

**Call 1 — Analysis:** *"Here is the current best program (score 2.38) and the three most recent attempts. What patterns do you observe about what works and what doesn't? What specific aspect of the algorithm is the current bottleneck? Formulate a concrete hypothesis about what change would produce the largest improvement."*

**Call 2 — Implementation:** *"Your hypothesis was: [hypothesis from Call 1]. Write code that tests this hypothesis. The code should be designed to either confirm or refute the hypothesis based on its score."*

The hypothesis itself gets stored in the database alongside the program's metrics and included in future prompts. Over time, the population accumulates not just programs but *knowledge about the problem* — which hypotheses were confirmed, which were refuted, and what remains unexplored.

ReEvo demonstrated that these "verbal gradients" significantly improve convergence on combinatorial optimization problems. The theoretical argument is straightforward: making the LLM's reasoning explicit allows it to be accumulated and shared across the population, converting the search from memoryless sampling to informed, directed exploration.

**Cost:** One additional LLM call per iteration (approximately doubling LLM cost). **Expected benefit:** Faster convergence and fewer wasted iterations on approaches that have already been tried.

#### 2. Landscape-Aware Prompting via MAP-Elites Analysis

The MAP-Elites grid contains rich information about the search landscape that is currently invisible to the LLM. Each cell represents a behavioral niche; the *pattern* of which cells are occupied, which are empty, and how scores vary across the grid encodes the structure of the problem.

**Concrete proposal:** Before building the prompt, analyze the current island's feature map and generate a landscape summary:

*"The MAP-Elites grid shows: High-complexity, high-diversity programs (cells 7-9, 7-9) average 0.82. Low-complexity programs (cells 0-3) average 0.65 but cell (2, 8) has an outlier at 0.91 — a concise program with unusual structure. The region (4-6, 4-6) is completely unexplored. Adjacent cells to the current program show a gradient toward higher complexity."*

This gives the LLM a map of where good solutions live, where unexplored territory exists, and what the local gradient looks like. It's analogous to providing the acquisition function signal in Bayesian optimization — helping the LLM decide whether to exploit (improve on the current best) or explore (target an empty region of the grid).

**Cost:** Negligible (pure computation on existing data). **Expected benefit:** More directed exploration, fewer redundant attempts in well-explored regions.

#### 3. Causal Analysis of Score Improvements

When a new best program is found, the system currently records *that* it improved but not *why*. A causal analysis step would:

1. Diff the new best against its parent.
2. Ask the LLM to classify the change: "Was this a parameter adjustment, structural refactoring, algorithmic shift, or library adoption?"
3. If the change is decomposable, ablate it: apply only the structural change, only the parameter change, etc., and evaluate each.
4. Store the result: "Switching from concentric rings to hexagonal placement accounted for 80% of the improvement; the remaining 20% came from the radius optimization tweak."

This builds a **library of productive mutations** — a set of change types that have historically produced improvements in this problem domain. Future prompts could include: "The following types of changes have been most productive: algorithmic shifts (+0.15 avg), boundary optimization (+0.08 avg), parameter tuning (+0.02 avg)."

This is related to the idea of "emitters" in CMA-MAP-Elites (Fontaine et al., 2019), where each emitter maintains a different search distribution. Instead of maintaining explicit distributions, the causal library serves as a natural-language summary of which search directions are productive.

**Cost:** 1-2 additional LLM calls + evaluation calls per improvement event (rare — only 3-13 per 100 iterations in our runs). **Expected benefit:** Accumulated problem-specific knowledge that directs future search.

#### 4. Multi-Objective Decomposition

The current evaluator returns a single `combined_score`, but the circle packing problem has at least three distinct aspects: boundary utilization, interior density, and constraint satisfaction. Decomposing the score into components and presenting them to the LLM would give it more signal:

*"Your program scored 0.87 overall. Breakdown: boundary utilization 0.92 (good — circles are close to edges), interior density 0.78 (weak — gaps in the center), constraint margin 0.99 (excellent — no near-overlaps). The interior density is the bottleneck."*

This is straightforward to implement in the evaluator (return sub-metrics) and the prompt (format them). The theoretical connection is to multi-objective optimization — instead of searching on a scalar, the LLM can reason about which objective to target. Combined with the MAP-Elites grid (which could use these sub-metrics as feature dimensions), this creates a richer landscape for the search to navigate.

**Cost:** Evaluator changes + prompt formatting. **Expected benefit:** More targeted improvements, fewer iterations wasted improving aspects that aren't the bottleneck.

#### 5. Adaptive Emitters via Online Model Specialization

CMA-MAP-Elites demonstrated that maintaining multiple search distributions ("emitters"), each adapting to a different region of the quality-diversity landscape, dramatically outperforms a single global search strategy. OpenEvolve's ensemble is a crude version of this — multiple models with fixed weights — but it doesn't adapt.

The deeper idea: use the evolution trace to identify which model (or which *prompting strategy*) produces the best results in which region of the MAP-Elites grid. Perhaps Opus excels at algorithmic shifts (discovering scipy.optimize) while Sonnet excels at parameter refinement. Route future iterations accordingly:

- Track per-model success rates *per region of the feature map*.
- Adjust sampling weights dynamically: if a model consistently produces improvements in high-complexity cells but failures in low-complexity cells, upweight it for high-complexity targets.
- This is the QD equivalent of bandit-based emitter selection (Cully, 2020).

**Cost:** Logging infrastructure + weight update logic. **Expected benefit:** Better utilization of heterogeneous model capabilities.

#### 6. Towards Bayesian Optimization in Program Space

The most theoretically ambitious direction. No existing system formalizes LLM-guided code evolution as Bayesian optimization, but the pieces are there:

- **Prior:** The LLM's learned distribution over programs.
- **Observations:** The MAP-Elites archive — a set of (program, score) pairs.
- **Surrogate model:** Missing. Could be built using LLM embeddings of programs as features, with a Gaussian process or neural network predicting scores from embeddings.
- **Acquisition function:** Missing. Could use expected improvement, upper confidence bound, or Thompson sampling over the surrogate.

The surrogate model would answer: "Given the programs we've evaluated so far, which *region of program space* is most likely to contain an improvement?" This is strictly more informative than the current approach of sampling from the LLM's prior conditioned on a few examples.

The practical challenge is that program space is discrete and high-dimensional — traditional BO doesn't work well. But recent work on embedding-based BO (using neural network embeddings to create continuous representations of discrete spaces) suggests this is tractable. The LLM itself provides the embedding via its hidden states.

**Cost:** Significant engineering and research. **Expected benefit:** Principled exploration-exploitation tradeoff, sample-efficient search.

### The Deeper Question: Learning to Search

All of the above proposals treat the LLM as a fixed (if better-prompted) component. The most fundamental limitation is that **the LLM never learns from the evolutionary process itself.** Surina et al. (2025) propose augmenting LLM-based evolutionary search with RL fine-tuning — using the evolution trace as training data to improve the LLM's mutation proposals over time.

This is the natural endpoint: a system that not only searches for good programs but *gets better at searching* as it accumulates experience. The evolution trace (parent program, mutation, child program, score delta) is exactly the data needed for supervised or reinforcement learning. Even without fine-tuning, one could distill the evolution trace into a "search strategy document" that is prepended to future prompts — a form of in-context meta-learning.

Our runs provide evidence this would help: Sonnet 4.5 + Opus 4.5 made 13 improvements in Phase 1, each building on the last. But the LLM wasn't *aware* it was building — it simply happened to produce improvements when given good examples. A system that explicitly tracked "which types of changes produce improvements on this problem" and biased future generations accordingly would likely converge faster with any model.

---

## 5. Practical Changes (Short-Term)

### Change 1: Make code extraction robust

**What:** Replace the raw-text fallback in `parse_full_rewrite` with a heuristic check or return `None` to discard responses without code blocks.

**Why:** Highest-impact, lowest-effort fix. With `gemini-2.5-flash`, 100% of iterations produced garbage because thinking output was treated as Python. This determines whether an entire class of models (thinking models, verbose models) can be used at all. ~5 lines of code.

### Change 2: Feed pre-evaluation failures back into prompts

**What:** When a program fails due to length limits, parsing failures, import errors, or missing functions, include a summary in the "Previous Attempts" section of the next prompt for that lineage.

**Why:** Gemini repeated `import scipy` 30 times. Claude Sonnet 4 repeated the `run_packing` error 20 times. The infrastructure already exists. ~30 lines in `process_parallel.py`. Notably, Sonnet 4.5 + Opus 4.5 mostly avoided these systematic errors through better instruction-following — but any new model or problem domain could reintroduce them.

### Change 3: Increase default evaluation timeout for complex problems

**What:** Make the evaluator timeout more generous or adaptive — perhaps starting high and reducing for programs that historically complete quickly.

**Why:** 34 timeouts in Run D's Phase 2 wasted over a third of iterations. The README's Anthropic config uses `timeout: 600` vs our 90. This is likely the single largest factor in our remaining gap from 96.9% to the README's 99.97%.

### Change 4: Split the Database class

**What:** Extract into `ProgramStore`, `IslandManager`, `MAPElitesGrid`, and `Sampler`.

**Why:** 1300 lines with 4-5 separate concerns. The sampling logic alone has three overlapping methods. Splitting would make it possible to swap strategies without touching serialization. Medium effort, ~1-2 days.

---

## 6. Experimental Design

### Validating Change 1 (Robust code extraction)

**Experiment:** Run circle packing with `gemini-2.5-flash` (the thinking model that currently fails) before and after the fix. Control: same experiment with `gemini-2.5-flash-lite` to confirm no regression.

**Metrics:** % iterations producing valid Python (expect ~0% → >80%), best score in 100 iterations.

### Validating Change 2 (Failure feedback)

**Experiment:** Run circle packing 3x with and 3x without failure feedback, using Claude Sonnet 4 (which had the highest error rate at 74%).

**Metrics:** Count of repeated same-category failures (expect 20% → <5% for `run_packing`), score trajectory, wall-clock time to reach 0.80. **Key question:** Does failure feedback make the LLM overly conservative?

### Validating Change 3 (Increased timeout)

**Experiment:** Re-run Phase 2 with Sonnet 4.5 + Opus 4.5 using `timeout: 300` instead of 90.

**Metrics:** Number of timeouts (expect 34 → <5), best score (expect improvement beyond 0.9686), wall-clock time. Compare the best timed-out programs from the 90s run against the best completed programs to confirm they were worth waiting for.

### Validating Change 4 (Database refactor)

**Approach:** Existing unit tests pass unchanged. Full circle packing run before/after with identical random seed produces identical final scores and population stats. Property-based test: serialize → deserialize → serialize is idempotent.

---

## 7. Run Results

### Run A: Gemini 2.5 Flash Lite (Phases 1 & 2)

**Phase 1** — 100 iterations, 2.5 minutes, 4 workers / 4 islands

| Iteration | Score | Sum Radii | Delta |
|-----------|-------|-----------|-------|
| 0 (seed) | 0.3642 | 0.960 | — |
| 3 | 0.5325 | 1.403 | +0.1682 |
| 4 | 0.6363 | 1.677 | +0.1038 |
| 97 | 0.6421 | 1.692 | +0.0058 |

82% valid programs. 12 code-too-long failures, 6 runtime errors. Only 3 improvements total — rapid early gains, then 93 iterations of plateau. Best program: layered concentric-ring construction with iterative radius adjustment. Pure geometric heuristic.

**Phase 2 (no scipy)** — 100 iterations, 6 minutes, 4 workers / 5 islands

Best: 0.8428 (sum_radii = 2.221). 38% valid programs. The dominant failure was `ModuleNotFoundError: No module named 'scipy'` — 30 identical errors because the LLM followed the system prompt's suggestion to use scipy but it wasn't installed. Best program: adaptive void-filling with grid-based sampling.

**Phase 2 (scipy installed)** — 100 iterations, 4 minutes. Best: 0.7168 (sum_radii = 1.889). *Worse* than without scipy. Zero scipy mentions in the entire log — the model never attempted it. Confirmed that model capability, not environment, was the binding constraint.

### Run C: Claude Sonnet 4 + Haiku 3.5 (Phase 2 only)

100 iterations, 41 minutes, 4 workers / 5 islands. Started from Gemini Phase 1 best (0.6421).

| Iteration | Score | Sum Radii | Delta |
|-----------|-------|-----------|-------|
| 0 (seed) | 0.6421 | 1.692 | — |
| 7 | 0.6640 | 1.750 | +0.0218 |
| 14 | 0.7310 | 1.926 | +0.0670 |
| 52 | **0.8692** | **2.290** | +0.1382 |

Only 26% valid programs — the lowest of any run. 20 iterations lost to `module 'program' has no attribute 'run_packing'` (Claude rewrote the entire file structure), 24 timeouts (90s), 10 wrong return signatures. Best program: multi-phase deterministic approach with strategic placement, analytical gradient optimization, importance-weighted overlap resolution. It built its own optimizer from scratch rather than using scipy. Evaluation time: 80.6 seconds — nearly hitting the 90s limit.

### Run D: Sonnet 4.5 + Opus 4.5 (Both Phases)

**Phase 1** — 100 iterations, 23 minutes, 4 workers / 4 islands

| Iteration | Score | Sum Radii | Delta |
|-----------|-------|-----------|-------|
| 0 (seed) | 0.3642 | 0.960 | — |
| 1 | 0.5463 | 1.440 | +0.1821 |
| 3 | 0.7407 | 1.952 | +0.1944 |
| 8 | 0.7653 | 2.017 | +0.0246 |
| 15 | 0.8199 | 2.160 | +0.0546 |
| 22 | 0.8224 | 2.167 | +0.0025 |
| 30 | 0.8235 | 2.170 | +0.0011 |
| 35 | 0.8482 | 2.235 | +0.0247 |
| 34 | 0.8624 | 2.272 | +0.0142 |
| 32 | 0.8688 | 2.289 | +0.0064 |
| 57 | 0.8786 | 2.315 | +0.0098 |
| 62 | 0.8806 | 2.320 | +0.0020 |
| 69 | 0.8830 | 2.327 | +0.0024 |
| 88 | **0.9040** | **2.382** | +0.0210 |

**98% valid programs** — only 2 runtime errors in 100 iterations. Zero timeouts. 13 best-program improvements spread across the full run, with gains still occurring at iteration 88. The score climbed steadily rather than plateauing. All four islands independently reached scores above 0.79, with the best island averaging 0.84. Phase 1 alone surpassed everything Gemini achieved across both phases.

**Phase 2** — 100 iterations, 65 minutes, 4 workers / 5 islands

| Iteration | Score | Sum Radii | Delta |
|-----------|-------|-----------|-------|
| 0 (seed) | 0.9040 | 2.382 | — |
| 39 | 0.9166 | 2.415 | +0.0126 |
| 45 | 0.9343 | 2.462 | +0.0177 |
| 59 | 0.9418 | 2.482 | +0.0075 |
| 71 | 0.9458 | 2.492 | +0.0040 |
| 81 | 0.9684 | 2.552 | +0.0226 |
| 90 | **0.9686** | **2.552** | +0.0002 |

34 timeouts, 6 other errors. The high timeout count reflects the models generating ambitious multi-stage optimization programs — the best program took 48.4 seconds to evaluate. All 5 islands reached scores above 0.92. Island 4 held the best program (0.9686) with the most programs (16) and lowest diversity (681) — it converged. Island 2 had the highest diversity (1769) while still scoring 0.9535 — it explored effectively without sacrificing much quality.

Best program: a multi-configuration evolutionary approach that tries 5 diverse initial layouts (literature-inspired, maximal-boundary, pressure-balanced rings, optimized hex, hybrid asymmetric), runs multi-stage physics-based optimization on each with momentum, adaptive step sizes, and progressive refinement, then ultra-fine-tunes the best result. A sophisticated meta-strategy that the weaker models never discovered.

---

## 8. Comparison to README's Reported Results

The README reports a two-phase evolution over 470 generations achieving **sum_radii = 2.634** (99.97% of the 2.635 target) using `claude-sonnet-4-5` + `claude-opus-4-5`.

### Score Comparison Across All Runs

| Configuration | Phase | Best Sum Radii | % Target | Valid % |
|--------------|-------|---------------|----------|---------|
| gemini-2.5-flash-lite | P1 | 1.692 | 64.2% | 82% |
| gemini-2.5-flash-lite | P2 | 2.221 | 84.3% | 38% |
| Claude Sonnet 4 + Haiku 3.5 | P2 | 2.290 | 86.9% | 26% |
| **Sonnet 4.5 + Opus 4.5** | **P1** | **2.382** | **90.4%** | **98%** |
| **Sonnet 4.5 + Opus 4.5** | **P2** | **2.552** | **96.9%** | ~62% |
| README (Sonnet 4.5 + Opus 4.5) | P2 (470 gen) | **2.634** | **99.97%** | — |

### Did We Reproduce the Expected Behavior?

**Yes, qualitatively. Nearly, quantitatively.** With the same models as the README (Sonnet 4.5 + Opus 4.5), we reached **96.9% of target** in 200 iterations vs the README's **99.97% in 470 iterations**. The remaining 3.1% gap (sum_radii 2.552 vs 2.634) is consistent with our shorter iteration budget and lower evaluation timeout.

**What reproduced exactly:**
- **Phase 1 trajectory shape** — rapid early improvement (0.36 → 0.74 by iteration 3) followed by steady gains through the full 100 iterations. The README describes the same pattern.
- **Phase 2 breaking through** — every configuration showed Phase 2 surpassing Phase 1's ceiling. With Sonnet 4.5, the gain was 0.9040 → 0.9686.
- **Algorithm transitions** — evolution progressed from concentric rings → hexagonal patterns → grid-based layouts → multi-configuration meta-strategies with physics-based optimization. The README describes the same trajectory: geometric heuristics → numerical optimization.
- **Island specialization** — all runs showed islands differentiating, with some converging and others maintaining high diversity. Run D's Phase 2 showed all 5 islands above 0.92 with diversity ranging from 681 to 1769.
- **Model quality as the dominant variable** — when we matched the README's models, we matched the README's approximate performance level.

**Why we fell 3.1% short:**

1. **Iteration budget.** We ran 200 total iterations; the README ran 470. The README's scipy.optimize breakthrough came at generation ~460. Our Run D was still improving at iteration 90 of Phase 2 — the score hadn't converged. More iterations would likely close the gap.

2. **Evaluation timeout.** Our Phase 2 used `timeout: 90`; the README's Anthropic config uses `timeout: 600`. Run D had 34 timeouts — over a third of Phase 2 iterations. Some of those timed-out programs may have been the scipy-class breakthroughs that push from 2.55 to 2.63. This is likely the single largest fixable factor.

3. **No scipy discovery (yet).** The README's final solution uses `scipy.optimize.minimize` with SLSQP. Our best program built its own multi-stage optimizer from scratch — conceptually similar but less mathematically efficient. With more iterations or a longer timeout, the LLM might discover the scipy formulation.

### What We Learned About OpenEvolve

The most important finding from running 5 configurations is that **OpenEvolve's framework is sound but model-sensitive**. The architecture (islands, MAP-Elites, cascade evaluation, process isolation) works correctly and as designed across all model qualities. But the quality of the LLM is the single largest lever:

- **Gemini 2.5 Flash Lite**: 64% → 84% of target. Fast, cheap, high validity rate, but incapable of algorithmic innovation. Plateaus quickly.
- **Claude Sonnet 4**: 87% of target. More ambitious code, lower validity rate (26%), but finds better algorithms. High error/timeout waste.
- **Sonnet 4.5 + Opus 4.5**: 97% of target. Near-perfect validity (98% in Phase 1), steady improvement across the full run, sophisticated multi-strategy approaches. The framework barely needed to compensate for model failures.

The framework's weaknesses (fragile code extraction, no failure feedback, timeout ceilings) matter most with *mid-tier* models that are capable enough to be ambitious but not reliable enough to be consistently correct. The strongest models largely route around these issues through better instruction-following. The weakest models never trigger them because they don't attempt anything complex enough to fail interestingly.

---

## Appendix: Environment & Setup Notes

- **Python**: 3.12.13 (conda-forge)
- **Gemini runs**: `gemini-2.5-flash-lite` via Google AI Studio (`generativelanguage.googleapis.com`). API key via `${GEMINI_API_KEY}`. The base `gemini-2.0-flash` alias is deprecated for new users. The thinking model `gemini-2.5-flash` breaks `parse_full_rewrite`'s fallback.
- **Claude runs**: via AWS Bedrock, proxied through LiteLLM (`http://127.0.0.1:4000/v1`). Auth via `AWS_BEARER_TOKEN_BEDROCK`. Required `litellm_settings: drop_params: true` (Bedrock rejects `seed`), and `top_p` must be removed when `temperature` is set (Bedrock rejects both simultaneously).
- **Original configs**: reference OpenRouter (`openrouter.ai/api/v1`) with `google/gemini-2.0-flash-001` naming. Model names must be adjusted per provider.
- **scipy**: Must be installed for optimization-based approaches. With weaker models, availability alone is insufficient — the LLM must be capable of formulating the constrained optimization problem.
- **LiteLLM proxy config** (`/tmp/litellm_config.yaml`): Maps model names to Bedrock model IDs. Must be running for Claude-based runs.
