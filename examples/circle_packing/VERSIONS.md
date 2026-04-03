# Experiment Versions

## v0: Baseline
- **Config:** `onsite_baseline.yaml`
- **Jobs:** `baseline_runs/run_{1..10}` (seeds 1-10)
- **Model:** gpt-5.4-mini
- **Key settings:** `hypothesis_driven: false`, timeout 90s, 100 iterations, 4 workers, 5 islands
- **Template:** `full_rewrite_user.txt` (standard "improve fitness score" prompt)
- **What:** Unmodified OpenEvolve with the default full-rewrite prompt.

## v0.1: Baseline + Constraints
- **Config:** `onsite_baseline_constrained.yaml`
- **Jobs:** `baseline_constrained_runs/run_{1..10}` (seeds 1-10)
- **Key settings:** `hypothesis_driven: false`, timeout 90s, custom `template_dir: templates_constrained`
- **Template:** `templates_constrained/full_rewrite_user.txt` (standard prompt + CONSTRAINTS block including timing limit)
- **What:** Same as v0 + explicit constraints in prompt. Tests constraint impact alone.

## v0.2: Baseline + Timeout 300s
- **Config:** `onsite_baseline_v02.yaml`
- **Jobs:** `baseline_v02_runs/run_{1..10}` (seeds 1-10)
- **Key settings:** `hypothesis_driven: false`, timeout **300s**
- **Template:** `full_rewrite_user.txt` (same as v0)
- **What:** Same as v0 but with 300s evaluation timeout (vs 90s). Tests whether longer timeout alone improves scores by allowing heavy optimization programs to complete.

## v1: HDE (Hypothesis-Driven Evolution)
- **Config:** `onsite_hde_v1.yaml`
- **Jobs:** `hde_v1_runs/run_{1..10}` (seeds 1-10)
- **Key settings:** `hypothesis_driven: true`, timeout 90s
- **Template:** `full_rewrite_user_hde.txt` (structured hypothesis prompt, no constraints)
- **What:** LLM states `**Hypothesis:**` before code. Hypotheses extracted, outcomes tracked, knowledge base shown in future prompts.
- **Changes from v0:** HDE prompt template, hypothesis extraction, knowledge base accumulation.

## v1.1: HDE + Constraints + Error Feedback (first attempt)
- **Config:** `onsite_hde_v2.yaml`
- **Jobs:** `hde_v2_runs/run_{1..10}` (seeds 1-10)
- **Key settings:** `hypothesis_driven: true`, timeout 90s
- **Template:** `full_rewrite_user_hde.txt` (with CONSTRAINTS block including timing limit)
- **What:** Added CONSTRAINTS block + error-only knowledge entries to v1.
- **Result:** Timeouts dropped 141→11 but scores dropped 0.875→0.814. Timing constraint was counterproductive (best programs NEED heavy computation). Error feedback didn't reach evaluated-but-invalid programs (bug: metrics errors not captured).

## v1.2: HDE + Fixed Constraints + Fixed Error Feedback + Timeout 300s
- **Config:** `onsite_hde_v12.yaml`
- **Jobs:** `hde_v12_runs/run_{1..10}` (seeds 1-10)
- **Key settings:** `hypothesis_driven: true`, timeout **300s**
- **Template:** `full_rewrite_user_hde.txt` (CONSTRAINTS without timing limit)
- **What:** Fixes from v1.1 analysis:
  1. Removed timing constraint from CONSTRAINTS (counterproductive)
  2. Fixed error feedback: now captures error reasons from evaluation metrics (e.g., "REFUTED (Invalid shapes)") not just from result.error
  3. Records errors even when no hypothesis was extracted (e.g., "[Evaluation failed: Invalid shapes]")
  4. Increased eval timeout to 300s so optimization-heavy programs can complete
- **Changes from v1.1:** Template fix, error feedback fix, timeout increase.

## v1.3: HDE + All Fixes + Prompt Cleanup
- **Config:** `onsite_hde_v13.yaml`
- **Jobs:** `hde_v13_runs/run_{1..10}` (seeds 1-10)
- **Key settings:** `hypothesis_driven: true`, timeout **300s**
- **Template:** `full_rewrite_user_hde.txt` (v1.3 fixes)
- **What:** Fixes three issues found in v1.2:
  1. Added explicit `run_packing()` requirement to CONSTRAINTS (28 errors in v1.2 from LLM dropping it)
  2. Removed `**bold**` markdown from hypothesis marker -- was triggering Unicode smart quotes in code output (12 errors in v1.2). Now uses plain `Hypothesis:` text.
  3. Added "Do not use any unicode characters in your code" constraint.
  4. Updated `extract_hypothesis()` regex to match both bold and plain markers.
- **Changes from v1.2:** Template wording only + regex update.

## Analysis
- **Baseline analysis:** `baseline_analysis.ipynb`
- **Comparison analysis:** `hde_comparison.ipynb`
