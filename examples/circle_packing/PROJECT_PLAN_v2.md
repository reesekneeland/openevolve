# HDE v2: Addressing Knowledge Base / Sampling Interaction

## Context

HDE v1 is fully implemented and functional. The core mechanism works: the prompt asks for a hypothesis, we extract it, track outcomes, and feed them back. But v1 has a design flaw we identified during analysis: the **knowledge base is global across all islands**, which leaks information across the isolation barriers that the island-based MAP-Elites algorithm deliberately maintains.

This matters because:
- An approach refuted on island 0 (e.g., bad implementation of scipy) gets labeled REFUTED for all islands, even though island 3 might have made it work with a different formulation
- Early noisy hypotheses (small population, unreliable signal) shape all future generations across all islands
- The directive "Do NOT repeat refuted approaches" is too strong — it converts advisory signal into a hard constraint

v2 fixes these issues and adds the infrastructure needed to properly evaluate HDE against the baseline.

## Changes (3 files, all surgical edits to existing code)

### 1. Per-Island Knowledge + Confidence Filtering

**File:** `openevolve/process_parallel.py`

**1a. Change knowledge_entries from flat list to per-island dict (line 396):**

Currently:
```python
self.knowledge_entries: list = []  # [(hypothesis, outcome, score_delta, iteration)]
```

Change to:
```python
self.knowledge_entries: dict = {i: [] for i in range(self.num_islands)}
# Per-island: [(hypothesis, outcome, score_delta, iteration)]
```

**1b. Record outcomes to the correct island (lines 790-810):**

After the existing outcome determination code, change the append:
```python
island_id = result.target_island if result.target_island is not None else 0
self.knowledge_entries[island_id].append(
    (result.hypothesis, h_outcome, h_delta, completed_iteration)
)
```

**1c. Snapshot sends the full per-island knowledge (lines 509-511):**

Currently sends all entries. Change to send per-island:
```python
if self.config.hypothesis_driven:
    snapshot["knowledge_entries"] = {
        str(k): v for k, v in self.knowledge_entries.items()
    }
```

**1d. Worker filters by island + applies confidence threshold (lines 184-195):**

Replace the current formatting block:
```python
if getattr(_worker_config, "hypothesis_driven", False):
    all_knowledge = db_snapshot.get("knowledge_entries", {})

    # Primary: this island's full knowledge history
    island_entries = all_knowledge.get(str(parent_island), [])

    # Secondary: confirmed hypotheses from other islands (cross-pollination)
    cross_island = []
    for isl, entries in all_knowledge.items():
        if int(isl) != parent_island:
            cross_island.extend(e for e in entries if e[1] == "CONFIRMED" and e[2] > 0.01)

    # Filter: only remove noise (|delta| < 0.005), keep everything else including all ERRORs
    island_entries = [e for e in island_entries if abs(e[2]) > 0.005 or e[1] == "ERROR"]

    # Show ALL filtered entries — no truncation
    display_entries = island_entries + cross_island

    if display_entries:
        kb_lines = ["# Knowledge Base (Past Hypotheses)"]
        for hyp, outcome, delta, it in display_entries:
            sign = "+" if delta > 0 else ""
            source = "" if (hyp, outcome, delta, it) in island_entries else " [other island]"
            kb_lines.append(f"- [{outcome} {sign}{delta:.4f}] \"{hyp}\" (iter {it}){source}")
        extra_prompt_kwargs["knowledge_base_summary"] = "\n".join(kb_lines)
    else:
        extra_prompt_kwargs["knowledge_base_summary"] = ""
```

Key design choices:
- Each island sees **its own full history** (no truncation)
- Plus **confirmed-only entries from other islands** — cross-pollination of successes without importing failures
- **Confidence threshold**: entries with |delta| < 0.005 are filtered out (noise), except ERROR entries which are always shown (failure feedback)

### 2. Softer Prompt Language

**File:** `openevolve/prompts/defaults/full_rewrite_user_hde.txt`

Change line 26 from:
```
Do NOT repeat approaches that were already refuted in the knowledge base.
```

To:
```
The knowledge base shows what has been tried. Avoid repeating approaches that produced errors. Refuted approaches may work with a different implementation — use your judgment.
```

This converts the hard ban into advisory guidance. The LLM can still try a "refuted" direction if it has a reason to believe a different implementation would work. ERRORs (import failures, missing functions) remain strongly discouraged since those are environment constraints, not implementation quality.

### 3. Log Knowledge Base State at Checkpoints

**File:** `openevolve/process_parallel.py`

In `run_evolution()`, where checkpoints are triggered (around line 668), add knowledge base logging:

```python
if checkpoint_callback:
    checkpoint_callback(completed_iteration)
    # Log knowledge base state for analysis
    total_entries = sum(len(v) for v in self.knowledge_entries.values())
    confirmed = sum(1 for v in self.knowledge_entries.values() for e in v if e[1] == "CONFIRMED")
    refuted = sum(1 for v in self.knowledge_entries.values() for e in v if e[1] == "REFUTED")
    errors = sum(1 for v in self.knowledge_entries.values() for e in v if e[1] == "ERROR")
    logger.info(
        f"Knowledge base: {total_entries} entries "
        f"({confirmed} confirmed, {refuted} refuted, {errors} errors)"
    )
```

This lets us see knowledge accumulation in the logs without any extra tooling.

## What This Does NOT Change

- `extract_hypothesis()` in `code_utils.py` — works fine as-is
- `SerializableResult.hypothesis` field — works fine as-is
- Config system — `hypothesis_driven: bool` flag is sufficient
- Template selection logic — works fine as-is
- No checkpoint persistence of knowledge base — still resets per run (acceptable for experiments)

## Verification

1. `python -m unittest discover tests` — no regressions
2. Run HDE v2 with `gpt-5.4-mini`, 100 iterations — confirm knowledge entries are logged per-island at checkpoints
3. Inspect log: confirm island-specific knowledge and cross-island confirmed entries appear in prompts
4. Run 5x baseline vs 5x HDE v2, same model/seeds — compare convergence, waste rate, final scores
5. Qualitative: read knowledge base entries from logs — are hypotheses reasonable? Do outcomes match reality?
