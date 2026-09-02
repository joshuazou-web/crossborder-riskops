"""Command line interface.

`python -m riskops demo` is the one command a reviewer needs: it builds the
warehouse, runs the risk engine, generates the briefs, seeds the review history
and prints what it did. Everything else is a smaller slice of that.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime

from .audit.log import AuditLog
from .config import get_settings
from .db import read_sql, session
from .pipeline.refresh import run_refresh, status_snapshot


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _print(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def cmd_demo(args: argparse.Namespace) -> int:
    settings = get_settings()
    print("Building CrossBorder RiskOps from synthetic data.")
    print(f"  seed        {settings.random_seed}")
    print(f"  as-of       {settings.as_of_date}")
    print(f"  provider    {settings.llm_provider} ({settings.llm_model})")
    print(f"  warehouse   {settings.db_path}")
    report = run_refresh(settings, seed=args.seed)
    _print(report.as_dict())
    if report.status != "success":
        return 1
    print("\nNext: python -m streamlit run app/Home.py")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    settings = get_settings()
    report = run_refresh(
        settings,
        seed=args.seed,
        generate_briefs=not args.no_briefs,
        seed_history=not args.no_history,
    )
    _print(report.as_dict())
    return 0 if report.status == "success" else 1


def cmd_status(_: argparse.Namespace) -> int:
    _print(status_snapshot(get_settings()))
    return 0


def cmd_cases(args: argparse.Namespace) -> int:
    settings = get_settings()
    with session(settings, read_only=True) as con:
        frame = read_sql(
            con,
            """
            SELECT case_id, transaction_id, risk_band, round(risk_score, 3) AS score,
                   policy_action, case_state, primary_reason_family, max_severity,
                   ai_recommended_action, round(ai_confidence, 2) AS ai_confidence
            FROM risk.cases
            WHERE (? = '' OR case_state = ?)
            ORDER BY risk_score DESC
            LIMIT ?
            """,
            [args.state or "", args.state or "", args.limit],
        )
    if frame.empty:
        print("No cases. Run `python -m riskops demo` first.")
        return 0
    print(frame.to_string(index=False))
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    """Show the stored AI brief for one case, or regenerate it."""
    from .ai.copilot import investigate, record_invocation
    from .ai.prompt import packet_from_frames
    from .ai.schema import CaseBrief

    settings = get_settings()
    with session(settings) as con:
        cases = read_sql(con, "SELECT * FROM risk.cases WHERE case_id = ?", [args.case_id])
        if cases.empty:
            print(f"case {args.case_id} not found")
            return 1
        if not args.regenerate:
            stored = read_sql(
                con,
                "SELECT brief_json FROM audit.ai_invocations WHERE case_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                [args.case_id],
            )
            if not stored.empty:
                print(CaseBrief.from_dict(json.loads(stored.iloc[0]["brief_json"])).to_json())
                return 0

        packet, allowed, gate = packet_from_frames(
            case_row=cases.iloc[0].to_dict(),
            transactions=read_sql(con, "SELECT * FROM core.transactions"),
            signals=read_sql(con, "SELECT * FROM risk.signals"),
            breaks=read_sql(con, "SELECT * FROM core.reconciliation_breaks"),
            merchants=read_sql(con, "SELECT * FROM core.merchants"),
            wallets=read_sql(con, "SELECT * FROM core.wallets"),
            model_scores=read_sql(con, "SELECT * FROM risk.model_scores"),
        )
        brief = investigate(
            settings,
            case_id=args.case_id,
            transaction_id=str(cases.iloc[0]["transaction_id"]),
            packet=packet,
            allowed_citations=allowed,
            input_gate=gate,
            requested_by="cli",
        )
        record_invocation(con, brief, packet=packet, requested_by="cli")
    print(brief.to_json())
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    from .review.workflow import submit_decision

    settings = get_settings()
    with session(settings) as con:
        result = submit_decision(
            con,
            case_id=args.case_id,
            actor_id=args.actor,
            actor_role=args.role,
            action=args.action,
            reason_code=args.reason,
            note=args.note,
            decided_at=datetime.now().replace(microsecond=0),
        )
    _print(result)
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    settings = get_settings()
    with session(settings, read_only=True) as con:
        log = AuditLog(con)
        chain = log.verify_chain()
        entries = log.entries()
    print(f"chain status: {chain.status}")
    print(chain.summary())
    if args.tail:
        print()
        print(
            entries.tail(args.tail)[
                ["seq", "occurred_at", "actor_role", "actor_id", "action", "object_id", "summary"]
            ].to_string(index=False)
        )
    return 0 if chain.valid else 1


def cmd_eval(args: argparse.Namespace) -> int:
    from .eval.runner import run_evaluation, write_report

    settings = get_settings()
    sweep_seeds = None
    if args.seeds:
        # Deterministic and reported: derived from the configured seed so the
        # sweep itself is reproducible, not a fresh set of random numbers.
        sweep_seeds = [settings.random_seed + offset * 7919 for offset in range(args.seeds)]
        print(f"Running a {args.seeds}-seed sweep. Each seed rebuilds a whole world; "
              "expect about a minute per seed.")
    results = run_evaluation(settings, sample_limit=args.limit, sweep_seeds=sweep_seeds)
    path = write_report(settings, results)
    print(json.dumps(results["headline"], indent=2))
    print(f"\nreport: {path}")
    print(f"json:   {settings.reports_dir / 'evaluation.json'}")
    return 0


def cmd_export(_: argparse.Namespace) -> int:
    settings = get_settings()
    target = settings.reports_dir / "marts_csv"
    target.mkdir(parents=True, exist_ok=True)
    with session(settings, read_only=True) as con:
        tables = read_sql(
            con,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'marts' "
            "ORDER BY table_name",
        )
        for name in tables["table_name"]:
            read_sql(con, f"SELECT * FROM marts.{name}").to_csv(
                target / f"{name}.csv", index=False
            )
    print(f"exported {len(tables)} mart tables to {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riskops",
        description="CrossBorder RiskOps - cross-border payment risk operations workbench "
                    "(all data synthetic)",
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="build everything from scratch")
    demo.add_argument("--seed", type=int, default=None)
    demo.set_defaults(func=cmd_demo)

    refresh = sub.add_parser("refresh", help="run one full refresh")
    refresh.add_argument("--seed", type=int, default=None)
    refresh.add_argument("--no-briefs", action="store_true", help="skip AI brief generation")
    refresh.add_argument("--no-history", action="store_true", help="skip simulated review history")
    refresh.set_defaults(func=cmd_refresh)

    status = sub.add_parser("status", help="row counts, refresh history, audit chain")
    status.set_defaults(func=cmd_status)

    cases = sub.add_parser("cases", help="list risk cases")
    cases.add_argument("--state", default="", help="filter by case_state")
    cases.add_argument("--limit", type=int, default=20)
    cases.set_defaults(func=cmd_cases)

    brief = sub.add_parser("brief", help="show or regenerate the AI brief for one case")
    brief.add_argument("case_id")
    brief.add_argument("--regenerate", action="store_true")
    brief.set_defaults(func=cmd_brief)

    decide = sub.add_parser("decide", help="commit a human decision on a case")
    decide.add_argument("case_id")
    decide.add_argument("--action", required=True,
                        choices=["release", "hold", "request_information", "escalate"])
    decide.add_argument("--actor", default="cli.user")
    decide.add_argument("--role", default="risk_analyst")
    decide.add_argument("--reason", default=None)
    decide.add_argument("--note", default="")
    decide.set_defaults(func=cmd_decide)

    audit = sub.add_parser("audit", help="verify the audit hash chain")
    audit.add_argument("--tail", type=int, default=10)
    audit.set_defaults(func=cmd_audit)

    evaluate = sub.add_parser("eval", help="run the evaluation harness and write the report")
    evaluate.add_argument("--limit", type=int, default=None,
                          help="cap the number of cases scored (for a quick run)")
    evaluate.add_argument("--seeds", type=int, default=0, metavar="N",
                          help="also run an N-seed robustness sweep (about a minute per seed); "
                               "without it the report quotes a single run and says so")
    evaluate.set_defaults(func=cmd_eval)

    export = sub.add_parser("export", help="export every mart table to CSV")
    export.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    _configure_logging(args.log_level or settings.log_level)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
