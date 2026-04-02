"""Zero-shot LLM baseline: sample GPT-5.4-mini 100 times with no evolution.

Measures the LLM's innate ability to solve circle packing from scratch.
No evolution, no feedback, no prior programs -- just "write a solution" x100.
"""

import json
import os
import re
import sys
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import openai
from openevolve.utils.code_utils import parse_full_rewrite

# Import evaluator functions
sys.path.insert(0, str(Path(__file__).parent))
from evaluator import run_with_timeout, validate_packing

PROMPT = """Write a Python program that packs 26 non-overlapping circles into a unit square [0,1]x[0,1] to maximize the sum of their radii. The AlphaEvolve paper achieved sum_radii=2.635 for n=26.

Your program must define exactly these functions:
- construct_packing() that returns (centers, radii, sum_radii) where centers is a numpy array of shape (26,2) and radii is a numpy array of shape (26,)
- run_packing() that calls construct_packing() and returns its result

Return ONLY the complete Python code in a ```python code block. No explanation needed.
You may use numpy and scipy."""

N_SAMPLES = 100
MODEL = "gpt-5.4-mini"
TARGET = 2.635
EVAL_TIMEOUT = 300


def evaluate_code(code, sample_id):
    """Write code to temp file and evaluate it."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        temp_path = f.name

    try:
        start = time.time()
        centers, radii, sum_radii = run_with_timeout(temp_path, timeout_seconds=EVAL_TIMEOUT)
        eval_time = time.time() - start

        if not isinstance(centers, np.ndarray):
            centers = np.array(centers)
        if not isinstance(radii, np.ndarray):
            radii = np.array(radii)

        shape_valid = centers.shape == (26, 2) and radii.shape == (26,)
        if not shape_valid:
            return {
                "sample_id": sample_id,
                "validity": 0.0,
                "combined_score": 0.0,
                "error": f"Invalid shapes: centers={centers.shape}, radii={radii.shape}",
                "eval_time": eval_time,
            }

        valid = validate_packing(centers, radii)
        actual_sum = float(np.sum(radii)) if valid else 0.0
        combined_score = (actual_sum / TARGET) if valid else 0.0

        return {
            "sample_id": sample_id,
            "validity": 1.0 if valid else 0.0,
            "sum_radii": actual_sum,
            "target_ratio": actual_sum / TARGET if valid else 0.0,
            "combined_score": combined_score,
            "eval_time": eval_time,
        }
    except Exception as e:
        return {
            "sample_id": sample_id,
            "validity": 0.0,
            "combined_score": 0.0,
            "error": str(e),
            "eval_time": 0.0,
        }
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def main():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set")
        sys.exit(1)

    client = openai.OpenAI(api_key=api_key)
    results = []
    total_tokens = 0

    print(f"Zero-shot baseline: {MODEL} x {N_SAMPLES} samples")
    print(f"Prompt: {len(PROMPT)} chars")
    print("=" * 60)

    for i in range(N_SAMPLES):
        t0 = time.time()

        # Call LLM
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role": "user", "content": PROMPT}],
                max_completion_tokens=8192,
                temperature=0.7,
            )
            content = response.choices[0].message.content
            usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
            total_tokens += usage["total_tokens"]
        except Exception as e:
            print(f"  [{i+1:3d}/{N_SAMPLES}] LLM ERROR: {e}")
            results.append({
                "sample_id": i,
                "validity": 0.0,
                "combined_score": 0.0,
                "error": f"LLM call failed: {e}",
                "eval_time": 0.0,
                "token_usage": {},
                "llm_time": time.time() - t0,
            })
            continue

        llm_time = time.time() - t0

        # Extract code
        code = parse_full_rewrite(content, "python")
        if not code:
            print(f"  [{i+1:3d}/{N_SAMPLES}] NO CODE EXTRACTED")
            results.append({
                "sample_id": i,
                "validity": 0.0,
                "combined_score": 0.0,
                "error": "No code block in response",
                "eval_time": 0.0,
                "token_usage": usage,
                "llm_time": llm_time,
            })
            continue

        # Evaluate
        result = evaluate_code(code, i)
        result["token_usage"] = usage
        result["llm_time"] = llm_time
        results.append(result)

        score = result.get("combined_score", 0)
        error = result.get("error", "")
        status = f"score={score:.4f}" if score > 0 else f"FAIL: {error[:50]}"
        print(f"  [{i+1:3d}/{N_SAMPLES}] {status}  ({usage['total_tokens']} tokens, {llm_time:.1f}s LLM, {result.get('eval_time', 0):.1f}s eval)")

    # Save results
    output_path = Path(__file__).parent / "zero_shot_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {output_path}")

    # Summary
    scores = [r["combined_score"] for r in results]
    valid_scores = [s for s in scores if s > 0]
    n_valid = len(valid_scores)

    print("\n" + "=" * 60)
    print(f"ZERO-SHOT BASELINE SUMMARY ({MODEL} x {N_SAMPLES})")
    print("=" * 60)
    print(f"  Valid programs:  {n_valid}/{N_SAMPLES} ({100*n_valid/N_SAMPLES:.1f}%)")
    if valid_scores:
        print(f"  Mean score:      {np.mean(valid_scores):.4f}")
        print(f"  Median score:    {np.median(valid_scores):.4f}")
        print(f"  Std score:       {np.std(valid_scores):.4f}")
        print(f"  Min/Max:         {np.min(valid_scores):.4f} / {np.max(valid_scores):.4f}")
        print(f"  Best sum_radii:  {max(r.get('sum_radii', 0) for r in results):.4f}")
    print(f"  Total tokens:    {total_tokens:,}")
    print(f"  Avg tokens/call: {total_tokens/N_SAMPLES:,.0f}")

    # Histogram
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(16, 6))

        # All scores (including zeros)
        axes[0].hist(scores, bins=30, edgecolor="black", alpha=0.7, color="steelblue")
        axes[0].set_xlabel("Combined Score")
        axes[0].set_ylabel("Count")
        axes[0].set_title(f"Zero-Shot Score Distribution ({MODEL}, n={N_SAMPLES})")
        axes[0].axvline(x=np.mean(scores), color="red", linestyle="--",
                        label=f"Mean={np.mean(scores):.4f}")
        if valid_scores:
            axes[0].axvline(x=np.mean(valid_scores), color="orange", linestyle="--",
                            label=f"Mean (valid only)={np.mean(valid_scores):.4f}")
        axes[0].legend()

        # Valid scores only
        if valid_scores:
            axes[1].hist(valid_scores, bins=25, edgecolor="black", alpha=0.7, color="mediumseagreen")
            axes[1].set_xlabel("Combined Score")
            axes[1].set_ylabel("Count")
            axes[1].set_title(f"Valid Programs Only (n={n_valid}/{N_SAMPLES})")
            axes[1].axvline(x=np.mean(valid_scores), color="red", linestyle="--",
                            label=f"Mean={np.mean(valid_scores):.4f}")
            axes[1].legend()

        plt.tight_layout()
        hist_path = Path(__file__).parent / "zero_shot_histogram.png"
        plt.savefig(hist_path, dpi=150, bbox_inches="tight")
        print(f"  Histogram saved to {hist_path}")
    except ImportError:
        print("  (matplotlib not available, skipping histogram)")


if __name__ == "__main__":
    main()
