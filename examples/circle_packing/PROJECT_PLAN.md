# Project Plan: Scientific Evolution Loop for OpenEvolve

## Context

We're building an improved AI scientist system on top of OpenEvolve for a 1-day trial at Intology. OpenEvolve currently treats the LLM as a memoryless mutation operator — each iteration says "improve this program" with no structured reasoning, no hypothesis formation, and no accumulated knowledge. Our runs (5 configurations, 500+ total iterations) revealed that:

1. **The LLM repeats the same mistakes indefinitely** — 30 identical scipy import errors, 20 identical `run_packing` errors — because pre-evaluation failures never feed back into prompts.
2. **The LLM doesn't reason about *why* to make changes** — it gets "improve this" and generates code. No explicit analysis of bottlenecks, no hypothesis about what would help, no learning from outcomes.
3. **Model quality dominates** — Sonnet 4.5 reached 97% of target vs Gemini's 64%. But even strong models waste 34% of iterations on timeouts/errors that could be avoided with better feedback.

The core insight: **the LLM should be treated as a scientist, not a code generator.** A scientist formulates hypotheses, designs experiments, analyzes results, and accumulates knowledge. The current system skips all of this.

## What We're Building: Hypothesis-Driven Evolution (HDE)

A modified evolution loop where each iteration follows the scientific method:

**Current flow:** Sample parent → "Improve this" → Code → Evaluate → Store

**New flow:** Sample parent → "Analyze bottlenecks, review past hypotheses, formulate a new hypothesis" → "Implement your hypothesis as code" → Evaluate → "Was the hypothesis confirmed?" → Store hypothesis + outcome in knowledge base → Feed back into future prompts

This is implemented as a **single LLM call with structured output** (not multiple calls), so there's zero latency overhead. The prompt asks the LLM to write its hypothesis *before* the code, and we parse both from the response.

## Implementation Plan

### Phase 1: Core Implementation (~3 hours)

#### Step 1: Hypothesis-Driven Prompt Template
**File:** `openevolve/prompts/defaults/full_rewrite_user.txt` (modify)

Replace the generic "Rewrite the program to improve its FITNESS SCORE" task section with a structured scientific prompt:

```
# Scientific Analysis
{knowledge_base_summary}

# Task
You are conducting a scientific experiment to improve this program's fitness score.

Step 1 - ANALYZE: What are the current program's strengths and weaknesses?
Step 2 - HYPOTHESIZE: Based on your analysis and the knowledge base above,
         state a specific, testable hypothesis about what change will improve fitness.
         Format: **Hypothesis:** [your hypothesis]
Step 3 - IMPLEMENT: Write the complete program that tests your hypothesis.

IMPORTANT: Your hypothesis should be specific enough that the evaluation result
will clearly confirm or refute it. Avoid vague hypotheses like "make it better."

```{language}
# Your rewritten program here
```
```

Also add `{knowledge_base_summary}` placeholder to the template, formatted as a section showing recent hypotheses and their outcomes.

#### Step 2: Hypothesis Extraction
**File:** `openevolve/utils/code_utils.py` (add function)

Add `extract_hypothesis(llm_response: str) -> Optional[str]` that extracts the hypothesis text from the LLM response before the code block. Pattern: find `**Hypothesis:**` marker, extract text until the next code block.

#### Step 3: Knowledge Base Data Structure
**File:** `openevolve/knowledge_base.py` (new file)

Simple class:
```python
@dataclass
class HypothesisRecord:
    hypothesis: str
    iteration: int
    island: int
    outcome: str  # "confirmed", "refuted", "error", "timeout"
    score_delta: float  # child score - parent score
    parent_score: float
    child_score: float

class KnowledgeBase:
    def __init__(self):
        self.records: List[HypothesisRecord] = []

    def add(self, record: HypothesisRecord)
    def format_for_prompt(self, island: int, n: int = 10) -> str
    def to_dict(self) -> dict  # for serialization
    def from_dict(cls, data: dict) -> KnowledgeBase
```

The `format_for_prompt` method returns a formatted string showing the N most recent hypotheses for a given island, with outcomes. Something like:

```
## Knowledge Base (Recent Hypotheses)
1. [CONFIRMED +0.034] "Switching to hexagonal inner ring improves density" (iter 12)
2. [REFUTED -0.011] "Adding scipy.optimize would improve over manual optimization" (iter 15, error: ModuleNotFoundError)
3. [CONFIRMED +0.082] "Void-filling with adaptive grid sampling improves boundary utilization" (iter 23)
```

#### Step 4: Wire Into Worker
**File:** `openevolve/process_parallel.py` (modify)

Changes:
1. Add `hypothesis: Optional[str] = None` to `SerializableResult` (line 37)
2. In `_create_database_snapshot()`, include serialized knowledge base from controller
3. In `_run_iteration_worker()` (line 269-279, full rewrite path):
   - After LLM call, extract hypothesis via `extract_hypothesis(llm_response)`
   - Pass knowledge base summary to `build_prompt()` via the `**kwargs` mechanism
   - Store hypothesis in the returned `SerializableResult`
   - For error cases (code too long, parse failure, eval error), also return hypothesis + error info
4. In `run_evolution()` result handling (line 549-678):
   - Extract hypothesis from result
   - Determine outcome (confirmed/refuted/error based on score delta)
   - Add to knowledge base
   - For errors, add as "refuted" with the error message as context

#### Step 5: Knowledge Base in Controller
**File:** `openevolve/process_parallel.py` (modify `ProcessParallelController`)

Add `self.knowledge_base = KnowledgeBase()` to `__init__`.
Pass to snapshot in `_create_database_snapshot()`.
Update in `run_evolution()` when results arrive.
Checkpoint: serialize to JSON alongside database metadata.

#### Step 6: Prompt Sampler Integration
**File:** `openevolve/prompt/sampler.py` (modify)

Add `knowledge_base_summary: str = ""` to `build_prompt()`'s `**kwargs` passthrough. The template already supports `**kwargs` in `user_template.format()` (line 160). No signature change needed — just pass `knowledge_base_summary=kb.format_for_prompt(island)` from the worker.

#### Step 7: Failure Feedback (piggyback on knowledge base)
When a worker returns an error (code too long, parse failure, eval error), create a HypothesisRecord with:
- `hypothesis`: extracted from response (if available), or "Unknown approach"
- `outcome`: "error"
- `score_delta`: 0.0
- Error message in a `notes` field

This automatically solves the failure feedback problem — repeated errors show up in the knowledge base as refuted hypotheses, and the LLM sees them in future prompts.

### Phase 2: Experiments (~3 hours)

#### Experiment 1: Baseline vs HDE (Primary)
- **Baseline:** Original OpenEvolve, 100 iterations, circle packing, gemini-2.5-flash-lite
- **Treatment:** HDE-enabled OpenEvolve, 100 iterations, same model, same config
- **Metrics:**
  - Sample efficiency: iterations to reach 0.5, 0.6, 0.7, 0.8
  - Waste rate: % of iterations with errors
  - Improvement rate: # of best-program events
  - Final best score
- **Expected signal:** HDE should reduce waste rate (no repeated errors) and reach score thresholds in fewer iterations

#### Experiment 2: Width vs Depth
- **Width:** 100 iterations x 1 LLM call (standard)
- **Depth:** 50 iterations x 2 LLM calls (hypothesis call + implementation call, separate)
- Same total LLM token budget
- **Question:** Is it better to generate more programs (width) or reason more per program (depth)?
- **Note:** For this experiment, implement a true 2-call variant where hypothesis and implementation are separate LLM calls

#### Experiment 3: Model Scaling with HDE
- Run HDE with gemini-2.5-flash-lite vs Claude Sonnet 4.5
- **Question:** Does hypothesis-driven search help more with weaker models? (Prediction: yes — stronger models implicitly reason about hypotheses anyway)

### Phase 3: Analysis & Presentation (~2 hours)

#### Deliverables
1. **Convergence curves** — Score vs iteration for baseline vs HDE (both models)
2. **Waste rate comparison** — Bar chart of % wasted iterations by error type
3. **Knowledge base analysis** — Sample hypotheses from a run, which were confirmed/refuted
4. **Width vs depth tradeoff** — Score vs total LLM tokens
5. **Qualitative examples** — Show before/after prompts, how the knowledge base evolved

#### Presentation structure
1. The problem: LLMs as memoryless mutation operators
2. The insight: treat the LLM as a scientist
3. Implementation: hypothesis-driven evolution
4. Results: quantitative + qualitative
5. Follow-up: what we'd build next (landscape-aware prompting, causal analysis, Bayesian optimization in program space)

## Critical Files

| File | Action | Key Lines |
|------|--------|-----------|
| `openevolve/prompts/defaults/full_rewrite_user.txt` | Modify | Entire file — new template |
| `openevolve/utils/code_utils.py` | Add function | After line 121 — `extract_hypothesis()` |
| `openevolve/knowledge_base.py` | **Create** | New file — `KnowledgeBase` class |
| `openevolve/process_parallel.py` | Modify | Lines 24-37 (SerializableResult), 134-332 (worker), 442-470 (snapshot), 549-678 (result handling) |
| `openevolve/prompt/sampler.py` | Minor modify | Line 150-160 — pass knowledge summary via kwargs |
| `openevolve/config.py` | Minor modify | Add `scientific_mode: bool = False` to Config |

## Design Decisions

**Single LLM call, not multiple:** The hypothesis is extracted from structured output within one call. This avoids doubling latency and token cost. The width-vs-depth experiment separately tests the 2-call variant.

**Knowledge base is per-run, not per-island:** Islands should see each other's hypotheses (cross-pollination of ideas). But records are tagged with island for filtering.

**Failure feedback is a special case of hypothesis tracking:** Rather than building a separate failure feedback mechanism, errors become "refuted hypotheses" in the knowledge base. This keeps the design unified.

**Backward compatible:** `scientific_mode: false` by default. Existing configs work unchanged. The knowledge base is empty when not enabled, and the template falls back to the original if the placeholder isn't present.

## Verification

1. Run baseline (original OpenEvolve) on circle packing with gemini-2.5-flash-lite, 100 iterations. Record score trajectory.
2. Run HDE on circle packing with same model and config, 100 iterations. Record score trajectory.
3. Compare: convergence speed, waste rate, final score.
4. Inspect knowledge base contents — are hypotheses reasonable? Do outcomes match reality?
5. Run unit tests: `python -m unittest discover tests` — ensure no regressions.
