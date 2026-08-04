"""Bare CLI entry point: `quill 42` (ticket #33).

quill operates on the **current directory** — it is the target repo (preflight enforces a git
repo with a remote). There is no ``--dir`` / ``--repo``: the repo is cwd and the remote is derived
from it. ``--init`` writes a default ``quillfolio.toml``; ``--start-phase`` takes a configured phase
**id**;
``--resume`` picks up the latest run (guarded by a config-hash match); ``--update`` revises the
ticket's existing open PR (checks out its branch, primes every phase with its review comments)
instead of shipping from scratch. Every run unloads all models on exit — there is no
``--no-unload``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from quill import events
from quill.bootstrap import InitError, init_config, seed_personas
from quill.config import ConfigError, QuillfolioConfig, load_config
from quill.git_ops import GitOps, SubprocessRunner
from quill.loader import ModelLoadError, router_url
from quill.modelserver import make_model_server
from quill.pipeline import PipelineDeps, make_run_id, run_dir_for, run_pipeline
from quill.preflight import (
    PreflightError,
    check_gh,
    check_router,
    check_target_dir,
)
from quill.runctx import MODE_CREATE, MODE_UPDATE, BuildTest
from quill.runners import UnknownRunnerError, get_runner

# Exit codes: 0 done, 1 failed, 2 config halt / needs-decision / arg error, 3 stopped.
_EXIT_FOR = {
    events.RUN_DONE: 0,
    events.RUN_FAILED: 1,
    events.NEEDS_DECISION: 2,
    events.RUN_HALTED: 3,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="quill", description="Ship a GitHub ticket end-to-end.")
    parser.add_argument("ticket", type=int, nargs="?", help="GitHub issue / ticket number")
    parser.add_argument(
        "--init",
        action="store_true",
        help="write a default quillfolio.toml in the current repo, then exit",
    )
    parser.add_argument("--start-phase", help="resume from this configured phase id (not an int)")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume the latest halted run for this ticket from its saved state",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="revise the ticket's existing open PR: check out its branch and run the full "
        "pipeline against its review comments",
    )
    parser.add_argument("--workflow", help="named workflow from quillfolio.toml")
    parser.add_argument(
        "--server",
        default=os.environ.get("QUILL_SERVER"),
        help="run on a remote quill server instead of this machine (env: QUILL_SERVER)",
    )
    parser.add_argument(
        "--branch",
        help="branch the server should work on (default: this checkout's current branch)",
    )
    parser.add_argument(
        "--clear-prefix-cache",
        action="store_true",
        help=argparse.SUPPRESS,  # accepted for older scripts; every run now clears exactly once
    )
    args = parser.parse_args(argv)

    directory = os.getcwd()

    # --init: write the repo's config (and seed the shared persona library) and exit.
    if args.init:
        try:
            config_file = init_config(directory)
            personas, copied = seed_personas()
        except InitError as exc:
            print(f"quill: {exc}", file=sys.stderr)
            return 2
        if copied:
            print(f"quill: seeded {copied} default personas into {personas}.", file=sys.stderr)
        print(
            f"quill: created {config_file}. Fill in runner.kind + build.command/build.test, "
            "then run `quill <ticket>`."
        )
        return 0

    if args.ticket is None:
        print("quill: a ticket number is required (or pass --init).", file=sys.stderr)
        return 2
    if args.update and args.resume:
        # --resume replays a saved run from its halted phase; --update starts a fresh run primed
        # with PR feedback. Honouring both would mean resuming a run that never saw the feedback.
        print("quill: --update and --resume cannot be combined.", file=sys.stderr)
        return 2
    if args.update and args.workflow not in (None, "pr_update"):
        print("quill: --update cannot be combined with a different --workflow.", file=sys.stderr)
        return 2
    if args.server and (args.resume or args.start_phase):
        # Both replay local run state (`--resume` reads a saved state file, `--start-phase` skips
        # phases of a run this machine drove). A remote run has neither on this side.
        print(
            "quill: --resume and --start-phase are local-only; they cannot be combined "
            "with --server.",
            file=sys.stderr,
        )
        return 2
    if args.ticket <= 0:
        print(f"quill: ticket must be a positive issue number (got {args.ticket})", file=sys.stderr)
        return 2

    # Remote mode: the server owns gh, models, personas and the checkout. This side only needs
    # the repo's config and its origin remote, so the local preflight does not apply.
    if args.server:
        return _run_remote(args, directory)

    # Fail fast, cheapest first: gh ready, cwd is a git repo with a remote.
    try:
        check_gh()
        check_target_dir(directory)
    except PreflightError as exc:
        print(f"quill: {exc}", file=sys.stderr)
        return 2

    # Load config. A missing quillfolio.toml tells the user to run --init.
    try:
        config = load_config(directory)
        workflow_id = args.workflow or ("pr_update" if args.update else config.workflow_id)
        config = config.select_workflow(workflow_id)
        runner = get_runner(config.runner, directory=directory)
    except (ConfigError, UnknownRunnerError) as exc:
        print(f"quill: {exc}", file=sys.stderr)
        return 2

    # Resolve where to start + which run dir to use.
    run_id = make_run_id(args.ticket)
    start_phase: str | None = args.start_phase
    clear_prefix_cache = args.clear_prefix_cache
    if args.resume:
        from quill.runstate_file import ResumeError, resume_target

        try:
            run_id, start_phase, clear_prefix_cache = resume_target(config, args.ticket)
        except ResumeError as exc:
            print(f"quill: {exc}", file=sys.stderr)
            return 2
        print(
            f"quill: resuming ticket {args.ticket} (run {run_id}) from phase {start_phase}",
            file=sys.stderr,
        )
    elif start_phase is not None and start_phase not in config.phase_ids:
        print(
            f"quill: --start-phase '{start_phase}' is not a configured phase id "
            f"(choose from: {', '.join(config.phase_ids)})",
            file=sys.stderr,
        )
        return 2

    loader = make_model_server(config, clear_prefix_cache=clear_prefix_cache)

    # The chosen runner's CLI must be present; the model server must answer.
    try:
        runner.preflight()
        _check_backend(config, clear_prefix_cache=clear_prefix_cache)
    except PreflightError as exc:
        print(f"quill: {exc}", file=sys.stderr)
        return 2

    deps = PipelineDeps.with_runner(
        runner,
        loader=loader,
        git=GitOps(run=SubprocessRunner(directory=directory)),
        build_test=_build_test_runner(directory),
        on_tool_progress=_tool_progress,
    )

    run_dir = run_dir_for(config, run_id)
    run_log = _open_run_log(run_dir)
    on_event = _make_on_event(
        config, args.ticket, run_id, run_log, clear_prefix_cache=clear_prefix_cache
    )

    try:
        final = run_pipeline(
            args.ticket,
            directory=directory,
            start_phase=start_phase,
            run_id=run_id,
            mode=MODE_UPDATE if args.update else MODE_CREATE,
            workflow=config.workflow_id,
            clear_prefix_cache=clear_prefix_cache,
            deps=deps,
            on_event=on_event,
        )
    except ConfigError as exc:
        print(f"quill: {exc}", file=sys.stderr)
        return 2
    finally:
        # Always unload on exit (#33: no --no-unload). The loader is hardened so a
        # subsequent run still loads cleanly after an unload-all.
        loader.unload_all()
        if run_log is not None:
            run_log.close()

    # The final run_done/failed/halted/needs-decision event was already emitted to on_event and
    # printed by the styled console (with its reason/question), so no extra plain line here.
    etype = final.get("type")
    return _EXIT_FOR.get(str(etype), 1)


def _run_remote(args: argparse.Namespace, directory: str) -> int:
    """Drive the run on a remote server, rendering its events exactly as a local run's."""
    from quill.client import ClientError, run_remote

    try:
        final = run_remote(
            server=args.server,
            directory=directory,
            ticket=args.ticket,
            mode=MODE_UPDATE if args.update else MODE_CREATE,
            workflow=args.workflow or ("pr_update" if args.update else "ticket"),
            branch=args.branch,
            clear_prefix_cache=args.clear_prefix_cache,
            on_event=_print_remote_event,
        )
    except ClientError as exc:
        print(f"quill: {exc}", file=sys.stderr)
        return 2
    return _EXIT_FOR.get(str(final.get("type")), 1)


def _print_remote_event(event: dict[str, object]) -> None:
    """Render a remote event with the local console.

    The console is driven by events, not by the engine, so a run on another machine prints exactly
    as one driven here — apart from `queued`, which only remote runs can produce.
    """
    from quill.console import print_event

    if event.get("type") == "queued":
        position = event.get("position")
        print(f"quill: queued behind {position} run(s) on the server.", file=sys.stderr)
        return
    print_event(event)


def _check_backend(config: QuillfolioConfig, *, clear_prefix_cache: bool = False) -> None:
    """Fail fast if the configured model server isn't reachable/ready (dispatch on backend)."""
    if config.backend == "vllm":
        first_model = next((model for phase in config.phases for model in phase.models), "")
        loader = make_model_server(config, clear_prefix_cache=clear_prefix_cache)
        try:
            loader.load(first_model, config.model_load_seconds)
        except ModelLoadError as exc:
            raise PreflightError(str(exc)) from exc
    else:
        check_router(router_url())


def _build_test_runner(directory: str) -> BuildTest:
    from quill.mechanical import build_test_runner

    return build_test_runner(directory)


# Keys shown on the compact (terminal) line, in order.
_COMPACT_KEYS = ("type", "phase", "label", "verdict")


def _format_event(event: dict[str, object]) -> str:
    bits = [str(event.get(k, "")) for k in _COMPACT_KEYS]
    return "  ".join(b for b in bits if b)


def _format_event_verbose(event: dict[str, object]) -> str:
    from quill import events

    # The run plan is a preformatted multi-line block — write it verbatim, not as repr'd extras.
    if event.get("type") == events.RUN_PLAN:
        return str(event.get("summary", "run plan"))

    line = _format_event(event)
    extras = [
        f"{k}={_format_extra(k, event[k])}"
        for k in event
        if k not in _COMPACT_KEYS and k not in ("summary", "lines") and event[k] not in (None, "")
    ]
    return f"{line}  [{', '.join(extras)}]" if extras else line


def _format_extra(key: str, value: object) -> str:
    """One ``k=v`` payload value for the log line. The tool tally gets the same human rendering
    the console uses (``'edit ×24 · read ×31'``) instead of a raw dict repr; everything else is
    repr'd as before."""
    if key == "tools" and isinstance(value, dict) and value:
        from quill.console import format_tools

        return repr(format_tools(cast(dict[str, int], value)))
    return repr(value)


def _print_event(event: dict[str, object]) -> None:
    from quill.console import print_event

    print_event(event)


def _tool_progress(_phase: str, tally: dict[str, int], _stream_path: Path) -> None:
    """Tick the console's live tool counter. Injected into the engine's deps so the driver itself
    never imports the console (only the bare CLI has a terminal; the API service has none).

    ``_stream_path`` is unused here — the terminal only shows a tool count — but is part of the
    progress hook so the API service can read live token usage from the transcript."""
    from quill.console import show_progress

    show_progress(tally)


def _open_run_log(run_dir: Path) -> TextIO | None:
    """Open ``<run-dir>/pipeline-run.log`` for append, flushed for live tailing."""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        return (run_dir / "pipeline-run.log").open("a", encoding="utf-8")
    except OSError:
        return None


def _make_file_sink(log: TextIO) -> Callable[[dict[str, object]], None]:
    def sink(event: dict[str, object]) -> None:
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        line = _format_event_verbose(event)
        try:
            log.write(f"{ts}  {line}\n")
            log.flush()
        except OSError:
            pass  # logging is best-effort; never break a run on a write failure

    return sink


def _make_on_event(
    config: QuillfolioConfig,
    ticket: int,
    run_id: str,
    log: TextIO | None,
    *,
    clear_prefix_cache: bool = False,
) -> Callable[[dict[str, object]], None]:
    """on_event that prints each transition, appends to the run log, AND persists run state."""
    from quill.runstate_file import make_recorder

    if log is None:
        base = _print_event
    else:
        file_sink = _make_file_sink(log)

        def base(event: dict[str, object]) -> None:
            _print_event(event)
            file_sink(event)

    return make_recorder(config, ticket, run_id, base, clear_prefix_cache=clear_prefix_cache)


if __name__ == "__main__":
    raise SystemExit(main())
