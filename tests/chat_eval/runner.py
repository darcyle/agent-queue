"""Test orchestration and result collection for chat agent evaluation.

Can be run as a module: python -m tests.chat_eval.runner --output tests/chat_eval/results/
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from src.chat_agent import ChatAgent
from src.chat_providers.base import ChatProvider
from src.config import AppConfig
from src.orchestrator import Orchestrator

from tests.chat_eval.metrics import (
    EvalRunResult,
    TestCaseResult,
    TurnResult,
    aggregate_results,
    evaluate_turn,
    save_run,
)
from tests.chat_eval.recording_handler import RecordingCommandHandler
from tests.chat_eval.report import generate_report
from tests.chat_eval.test_cases._loader import load_all_cases
from tests.chat_eval.test_cases._types import TestCase


async def run_single_case(
    orchestrator: Orchestrator,
    config: AppConfig,
    provider: ChatProvider,
    case: TestCase,
) -> TestCaseResult:
    """Execute a single test case and evaluate results.

    Each case gets a fresh ChatAgent to prevent cross-contamination.
    """
    # Fresh agent per case
    agent = ChatAgent(orchestrator, config)
    agent._provider = provider

    recorder = RecordingCommandHandler(orchestrator, config)
    agent.handler = recorder

    case_result = TestCaseResult(
        case_id=case.id,
        category=case.category,
        difficulty=case.difficulty.value,
        tags=case.tags,
    )

    try:
        # Set case-level active project
        if case.active_project:
            agent.set_active_project(case.active_project)

        # Run setup commands
        for cmd_name, cmd_args in case.setup_commands:
            await recorder.execute(cmd_name, cmd_args)
        recorder.reset()  # Don't count setup calls

        # Run each turn
        history: list[dict] = []
        for turn in case.turns:
            # Override active project per-turn if specified
            if turn.active_project is not None:
                agent.set_active_project(turn.active_project)

            recorder.reset()
            start = time.monotonic()

            response = await agent.chat(turn.user_message, user_name="test_user", history=history)

            elapsed = time.monotonic() - start

            # Evaluate this turn
            turn_result = evaluate_turn(turn, recorder.calls)
            turn_result.latency = elapsed
            case_result.turn_results.append(turn_result)

            # Build history for multi-turn
            history.append({"role": "user", "content": turn.user_message})
            history.append({"role": "assistant", "content": response})

        # Case passes if all turns pass
        case_result.passed = all(tr.passed for tr in case_result.turn_results)

    except Exception as e:
        case_result.error = str(e)
        case_result.passed = False

    return case_result


async def run_eval(
    orchestrator: Orchestrator,
    config: AppConfig,
    cases: list[TestCase],
    provider: ChatProvider,
    model_name: str = "",
) -> EvalRunResult:
    """Run all cases and return aggregated results."""
    case_results: list[TestCaseResult] = []

    for case in cases:
        result = await run_single_case(orchestrator, config, provider, case)
        case_results.append(result)

    return aggregate_results(case_results, model=model_name or provider.model_name)


async def _main(args: argparse.Namespace) -> None:
    """CLI entry point for running evaluations."""
    import tempfile
    from pathlib import Path
    from src.chat_providers import create_chat_provider
    from src.config import ChatProviderConfig

    # Create temp dir for DB
    with tempfile.TemporaryDirectory() as tmp:
        config = AppConfig(
            database_path=str(Path(tmp) / "eval.db"),
            workspace_dir=str(Path(tmp) / "workspaces"),
            chat_provider=ChatProviderConfig(
                provider=args.provider,
                model=args.model,
            ),
        )

        # Import MockAdapterFactory locally to avoid circular deps at top level
        from tests.chat_eval.conftest import MockAdapterFactory

        orch = Orchestrator(config, adapter_factory=MockAdapterFactory())
        await orch.initialize()

        try:
            provider = create_chat_provider(config.chat_provider)
            if not provider:
                print("Failed to create chat provider. Check API key.", file=sys.stderr)
                sys.exit(1)

            cases = load_all_cases()
            if args.category:
                cases = [c for c in cases if c.category == args.category]
            if args.difficulty:
                cases = [c for c in cases if c.difficulty.value == args.difficulty]

            print(f"Running {len(cases)} test cases with {provider.model_name}...")
            run_result = await run_eval(orch, config, cases, provider)

            # Save results
            output_dir = Path(args.output)
            json_path = save_run(run_result, output_dir)
            print(f"Results saved to {json_path}")

            # Generate report
            report = generate_report(run_result)
            report_path = output_dir / f"report_{int(run_result.timestamp)}.md"
            report_path.write_text(report)
            print(f"Report saved to {report_path}")

            print(f"\nPass rate: {run_result.pass_rate:.1%} "
                  f"({run_result.passed_cases}/{run_result.total_cases})")

        finally:
            await orch.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Run chat agent evaluation")
    parser.add_argument("--output", default="tests/chat_eval/results/",
                        help="Output directory for results")
    parser.add_argument("--provider", default="anthropic",
                        help="Chat provider (anthropic, ollama)")
    parser.add_argument("--model", default="", help="Model name override")
    parser.add_argument("--category", default="", help="Filter by category")
    parser.add_argument("--difficulty", default="", help="Filter by difficulty")
    args = parser.parse_args()
    asyncio.run(_main(args))


if __name__ == "__main__":
    main()
