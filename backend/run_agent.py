#!/usr/bin/env python
"""Run the agent loop against a repo from the command line.

    python run_agent.py https://github.com/octocat/Hello-World "add a README badge"

Standalone on purpose: no database, no queue, no API. Phase 3 wires the same
loop into a Celery task.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import textwrap
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.llm import infer_provider  # noqa: E402
from agent.loop import DEFAULT_MAX_STEPS, DEFAULT_MODEL, AgentLoop  # noqa: E402
from agent.schemas import AgentStep  # noqa: E402
from agent.sandbox import SandboxError, sandbox_for  # noqa: E402
from agent.workspace import (  # noqa: E402
    clone_repo,
    collect_diff,
    collect_diff_via,
    collect_file_diffs,
    collect_file_diffs_via,
    prepare_git_identity,
)

RESET = "\033[0m"
COLORS = {"thought": "\033[36m", "action": "\033[33m", "observation": "\033[32m"}
DIM = "\033[2m"


def supports_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty() and os.name != "nt"


class StepPrinter:
    """Prints steps as they happen, so a long run is watchable."""

    def __init__(self, *, color: bool, max_chars: int = 1200) -> None:
        self.color = color
        self.max_chars = max_chars

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def __call__(self, step: AgentStep) -> None:
        label = f"[{step.step_number:>2}] {step.type.upper()}"
        if step.tool_name:
            label += f"  {step.tool_name}"
        print(self._paint(label, COLORS.get(step.type, "")))

        if step.tool_input:
            rendered = json.dumps(step.tool_input, indent=2)
            print(self._paint(self._clip(rendered), DIM))
        if step.content:
            print(textwrap.indent(self._clip(step.content), "    "))
        print()

    def _clip(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        return f"{text[: self.max_chars]}\n    ... [{len(text) - self.max_chars} more characters]"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Sandcastle coding agent against a git repository.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("repo", help="Git repo URL, or a local path when --local is given.")
    parser.add_argument("description", help="What the agent should do.")
    parser.add_argument("--local", action="store_true", help="Treat REPO as a local path to copy.")
    parser.add_argument("--model", default=os.getenv("AGENT_MODEL", DEFAULT_MODEL))
    parser.add_argument(
        "--provider",
        choices=["gemini", "anthropic"],
        default=os.getenv("LLM_PROVIDER") or None,
        help="Defaults to whatever the model name implies.",
    )
    parser.add_argument(
        "--max-steps", type=int, default=int(os.getenv("AGENT_MAX_STEPS", DEFAULT_MAX_STEPS))
    )
    parser.add_argument("--keep", action="store_true", help="Leave the workspace on disk.")
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Run inside a Docker sandbox instead of directly on this machine.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON instead.")
    return parser.parse_args(argv)


def prepare_workspace(args: argparse.Namespace, root: Path) -> Path:
    workspace = root / "repo"
    if args.local:
        source = Path(args.repo).expanduser().resolve()
        if not source.is_dir():
            raise SystemExit(f"error: {source} is not a directory")
        shutil.copytree(source, workspace)
        print(f"copied {source} -> {workspace}\n")
    else:
        print(f"cloning {args.repo} ...")
        clone_repo(args.repo, workspace)
        print(f"cloned to {workspace}\n")
    return workspace


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = parse_args(argv)

    provider = args.provider or infer_provider(args.model)
    required_key = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    if not os.getenv(required_key):
        hint = (
            " Get a free one at https://aistudio.google.com/apikey."
            if provider == "gemini"
            else ""
        )
        print(
            f"error: {required_key} is not set (provider={provider}). "
            f"Add it to .env or export it.{hint}",
            file=sys.stderr,
        )
        return 2

    root = Path(tempfile.mkdtemp(prefix="sandcastle-"))
    started = time.monotonic()
    try:
        workspace = prepare_workspace(args, root)

        printer = None if args.json else StepPrinter(color=supports_color(sys.stdout))
        if printer:
            print(f"task: {args.description}")
            print(f"model: {args.model} ({provider})   max_steps: {args.max_steps}\n")
            print("-" * 72, "\n")

        def drive(backend=None):
            loop = AgentLoop(
                workspace=None if backend is not None else workspace,
                description=args.description,
                repo_url=args.repo,
                model=args.model,
                provider=provider,
                max_steps=args.max_steps,
                backend=backend,
                on_step=printer,
            )
            return loop.run()

        if args.sandbox:
            try:
                with sandbox_for(f"cli-{os.getpid()}", source=workspace) as box:
                    backend = box.backend()
                    prepare_git_identity(backend)
                    result = drive(backend)
                    # The diff has to come out before the container does.
                    result.diff = collect_diff_via(backend)
                    file_diffs = collect_file_diffs_via(backend)
            except SandboxError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        else:
            result = drive()
            result.diff = collect_diff(workspace)
            file_diffs = collect_file_diffs(workspace)

        duration = time.monotonic() - started

        if args.json:
            print(
                json.dumps(
                    {
                        "steps": [s.to_dict() for s in result.steps],
                        "final_message": result.final_message,
                        "stop_reason": result.stop_reason,
                        "error": result.error,
                        "diff": result.diff,
                        "files_changed": [d.file_path for d in file_diffs],
                        "duration_seconds": round(duration, 2),
                        "usage": {
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                        },
                    },
                    indent=2,
                )
            )
        else:
            print("-" * 72, "\n")
            if result.final_message:
                print("SUMMARY\n")
                print(textwrap.indent(result.final_message, "  "), "\n")
            if result.error:
                print(f"ERROR: {result.error}\n", file=sys.stderr)

            print("DIFF\n")
            print(result.diff if result.diff.strip() else "  (no changes)")
            print()
            print(
                f"{result.step_count} steps | {len(file_diffs)} file(s) changed | "
                f"{duration:.1f}s | stop_reason={result.stop_reason} | "
                f"tokens in/out {result.input_tokens}/{result.output_tokens}"
            )

        if args.keep:
            print(f"\nworkspace kept at {workspace}", file=sys.stderr)
        return 1 if result.error else 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
