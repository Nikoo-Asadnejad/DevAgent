"""`devagent` command: `serve` (API + worker) or `check-config`."""

import argparse
import logging
import os
import sys
from pathlib import Path

from devagent.core.config import AgentConfig, ConfigError, load_config

log = logging.getLogger("devagent")


def serve(cfg: AgentConfig) -> None:
    import uvicorn

    from devagent.app.api import create_app
    from devagent.app.factories import (
        build_code_host,
        build_engine,
        build_tracker,
        collect_secrets,
    )
    from devagent.core.guardrails import Redactor
    from devagent.prompts import load_rules
    from devagent.app.worker import Worker
    from devagent.runtime.gitops import GitOps
    from devagent.runtime.sandbox import OpenHandsSandbox, reap_sandbox_containers
    from devagent.runtime.secretscan import GitleaksScanner
    from devagent.runtime.storage import open_storage
    from devagent.workflow import Deps, build_graph, start_run

    # Nothing can legitimately be running before the worker starts: remove sandboxes left by a previous crash.
    removed = reap_sandbox_containers(None, instance=cfg.sandbox.instance)
    if removed:
        log.warning("removed %d orphaned sandbox container(s)", removed)
    tracker = build_tracker(cfg)
    redactor = Redactor(collect_secrets(cfg, os.environ))
    with open_storage() as storage:
        store = storage.store
        deps = Deps(
            config=cfg,
            tracker=tracker,
            code_host=build_code_host(cfg),
            engine=build_engine(cfg, os.environ),
            sandbox=OpenHandsSandbox(cfg.sandbox),
            git=GitOps(
                author_name=cfg.git.author_name, author_email=cfg.git.author_email
            ),
            scanner=GitleaksScanner(),
            store=store,
            redactor=redactor,
            rules=load_rules(),
        )
        graph = build_graph(deps, storage.checkpointer)
        worker = Worker(
            cfg,
            tracker,
            store,
            start=lambda run: start_run(graph, run),
            redactor=redactor,
        )
        admin = cfg.service.admin_token
        app = create_app(
            worker=worker,
            store=store,
            admin_token=admin.get_secret_value() if admin else None,
        )
        worker.start()
        try:
            uvicorn.run(
                app, host=cfg.service.host, port=cfg.service.port, log_config=None
            )
        finally:
            worker.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="devagent")
    parser.add_argument("command", choices=["serve", "check-config"])
    parser.add_argument(
        "--config", default=os.environ.get("DEVAGENT_CONFIG", "config/agent.yaml")
    )
    args = parser.parse_args(argv)

    try:
        cfg = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"devagent: {exc}", file=sys.stderr)
        return 2

    from devagent.app.factories import collect_secrets
    from devagent.runtime.logging import configure_logging

    configure_logging(cfg.service.log_level, secrets=collect_secrets(cfg, os.environ))

    if args.command == "check-config":
        print(
            f"config OK: github, engine=codex, repos={len(cfg.repos)}, storage=memory"
        )
        return 0
    serve(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
