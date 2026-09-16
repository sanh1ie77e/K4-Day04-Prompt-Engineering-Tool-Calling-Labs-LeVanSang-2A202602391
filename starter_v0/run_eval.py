from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from env_loader import load_lab_env
from providers import make_provider
from tools import TOOL_FUNCTIONS, load_tool_declarations, to_openai_tools
from versioning import artifact_version_dict, build_artifact_version


ROOT = Path(__file__).parent
ARTIFACTS_DIR = ROOT / "artifacts"
load_lab_env(ROOT)


def run_case(
    *,
    provider: Any,
    openai_tools: list[dict[str, Any]],
    model: str | None,
    system_prompt: str,
    messages: list[dict[str, str]],
    max_tool_rounds: int,
) -> dict[str, Any]:
    """Run one eval case and return a result dict."""
    from chat import run_model_tool_loop

    full_messages = [{"role": "system", "content": system_prompt}, *messages]
    return run_model_tool_loop(
        provider=provider,
        messages=full_messages,
        tools=openai_tools,
        model=model,
        max_tool_rounds=max_tool_rounds,
    )


def score_case(
    result: dict[str, Any],
    expected_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare actual tool calls with expected, return scoring dict."""
    actual_calls = []
    for rnd in result.get("rounds", []):
        for call in rnd.get("tool_calls", []):
            actual_calls.append(call)

    expected_names = [c.get("tool") or c.get("name", "") for c in expected_calls]
    actual_names = [c.get("name", "") for c in actual_calls]

    routing_correct = expected_names == actual_names
    passed = routing_correct

    failures: list[str] = []
    if not routing_correct:
        failures.append(
            f"expected_tools={expected_names} actual_tools={actual_names}"
        )

    return {
        "passed": passed,
        "routing_correct": routing_correct,
        "args_correct": routing_correct,
        "actual_tool_calls": actual_calls,
        "failures": failures,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run eval suite against a live provider and save results."
    )
    parser.add_argument(
        "--provider",
        choices=["openrouter", "openai", "anthropic", "gemini"],
        required=True,
    )
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--version",
        required=True,
        help="Artifact version label, e.g. v0, v1.",
    )
    parser.add_argument(
        "--suite",
        default="base",
        help="Eval suite name (e.g. 'base', 'helpdesk_extension', 'adversarial', 'group') or path to JSON file.",
    )
    parser.add_argument(
        "--eval-cases",
        type=Path,
        default=None,
        help="Explicit path to eval cases JSON file (overrides --suite path).",
    )
    parser.add_argument(
        "--system-prompt",
        type=Path,
        default=ARTIFACTS_DIR / "system_prompt.md",
    )
    parser.add_argument(
        "--tools",
        type=Path,
        default=ARTIFACTS_DIR / "tools.yaml",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=ROOT / "runs",
    )
    parser.add_argument("--max-tool-rounds", type=int, default=4)
    args = parser.parse_args()

    # Resolve suite file path from --eval-cases or --suite
    suite_shortcuts = {
        "base": ROOT / "data" / "eval_base.json",
        "helpdesk_extension": ROOT / "data" / "eval_helpdesk_extension.json",
        "extension": ROOT / "data" / "eval_helpdesk_extension.json",
        "adversarial": ROOT / "data" / "eval_adversarial.json",
        "group": ROOT / "data" / "eval_group.json",
    }

    if args.eval_cases:
        suite_path = Path(args.eval_cases)
    else:
        suite_str = str(args.suite)
        if suite_str in suite_shortcuts:
            suite_path = suite_shortcuts[suite_str]
        elif Path(suite_str).exists():
            suite_path = Path(suite_str)
        else:
            # Check inside data/
            candidate = ROOT / "data" / f"eval_{suite_str}.json"
            if candidate.exists():
                suite_path = candidate
            else:
                suite_path = Path(suite_str)

    system_prompt = args.system_prompt.read_text(encoding="utf-8")
    tool_declarations = load_tool_declarations(args.tools)
    openai_tools = to_openai_tools(tool_declarations)
    provider = make_provider(args.provider)
    selected_model = args.model or getattr(provider, "default_model", None)
    artifact_version = build_artifact_version(
        args.version, args.system_prompt, args.tools
    )

    suite_data = json.loads(suite_path.read_text(encoding="utf-8"))
    cases = suite_data.get("cases", suite_data) if isinstance(suite_data, dict) else suite_data
    suite_id = suite_data.get("dataset_id", suite_path.stem) if isinstance(suite_data, dict) else suite_path.stem

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    run_id = f"{args.version}_{args.provider}_{timestamp}"

    results: list[dict[str, Any]] = []
    passed_count = 0

    print(f"Running eval: suite={suite_id}, provider={args.provider}, model={selected_model}")
    print(f"Cases: {len(cases)}")

    for case in cases:
        case_id = case.get("id", "?")
        turns = case.get("turns") or case.get("messages") or []

        # Extract expected calls from case['expect']['tool_calls'] or case['expected_calls']
        expect_obj = case.get("expect", {})
        if "tool_calls" in expect_obj:
            expected_calls = expect_obj.get("tool_calls", [])
        elif "expected_calls" in case:
            expected_calls = case.get("expected_calls", [])
        elif expect_obj.get("no_tool"):
            expected_calls = []
        else:
            expected_calls = []

        # Build messages list from turns or query
        messages: list[dict[str, str]] = []
        if turns:
            for turn in turns:
                role = turn.get("role", "user")
                content = turn.get("content", "")
                messages.append({"role": role, "content": content})
        elif "query" in case:
            messages = [{"role": "user", "content": case["query"]}]
        else:
            user_input = case.get("input") or case.get("user", "")
            messages = [{"role": "user", "content": user_input}]

        print(f"  Case {case_id}...", end=" ", flush=True)
        try:
            result = run_case(
                provider=provider,
                openai_tools=openai_tools,
                model=args.model,
                system_prompt=system_prompt,
                messages=messages,
                max_tool_rounds=args.max_tool_rounds,
            )
            score = score_case(result, expected_calls)
            if score["passed"]:
                passed_count += 1
                print("PASS")
            else:
                print("FAIL -", score["failures"])
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
            score = {
                "passed": False,
                "routing_correct": False,
                "args_correct": False,
                "actual_tool_calls": [],
                "failures": [str(exc)],
            }
            print(f"ERROR - {exc}")

        results.append({
            "id": case_id,
            "is_multiturn": len(messages) > 1,
            "expect": {"tool_calls": expected_calls},
            "result": {**score, "run_result": result},
        })

    total = len(cases)
    pass_rate = passed_count / total if total else 0
    print(f"\nResult: {passed_count}/{total} passed ({pass_rate:.0%})")

    run_record: dict[str, Any] = {
        "run_id": run_id,
        "suite": suite_id,
        **artifact_version_dict(artifact_version),
        "provider": args.provider,
        "model": selected_model,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "passed": passed_count,
        "total": total,
        "pass_rate": round(pass_rate, 4),
        "results": results,
    }

    args.runs_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.runs_dir / f"{run_id}.json"
    out_path.write_text(
        json.dumps(run_record, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"Run saved: {out_path}")


if __name__ == "__main__":
    main()
