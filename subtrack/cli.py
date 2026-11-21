"""CLI entry point for subtrack."""

from __future__ import annotations

import argparse
import sys
from typing import TextIO

from googleapiclient.errors import HttpError

from subtrack import config, db
from subtrack.gmail_client import (
    authenticate,
    build_queries,
    fetch_message,
    get_profile,
    get_service,
    parse_message,
    search_message_ids,
)
from subtrack.models import Email


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def cmd_auth(args: argparse.Namespace) -> None:
    """Run the OAuth flow and print the authenticated Gmail address."""
    creds = authenticate()
    service = get_service(creds)
    email = get_profile(service)
    print(email)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def load_processed_ids() -> set[str]:
    """Read processed IDs without creating the database if it doesn't exist."""
    if not config.DB_PATH.exists():
        return set()
    conn = db.connect(config.DB_PATH)
    try:
        return db.processed_ids(conn)
    finally:
        conn.close()


def _format_line(email: Email) -> str:
    # Sender is padded for alignment but never truncated: the domain at the
    # end is what tells a real receipt from a lookalike.
    date = email.received_at.astimezone().strftime("%Y-%m-%d")
    return f"{date}  {email.sender:<40}  {email.subject}"


def run_dry_sync(
    service,
    processed: set[str],
    lookback: str,
    limit: int | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> list[Email]:
    """Search, fetch and parse candidates, print them, and save nothing.

    Returns the parsed emails (in memory only).
    """
    out = out or sys.stdout
    err = err or sys.stderr
    queries = build_queries(lookback)
    ids, hits = search_message_ids(service, queries)

    print("Search hits per query:", file=out)
    for q in queries:
        print(f"  {hits[q]:>5}  {q}", file=out)

    new_ids = [i for i in ids if i not in processed]
    todo = new_ids[:limit] if limit else new_ids
    print(
        f"{len(ids)} unique messages, {len(ids) - len(new_ids)} already processed, "
        f"fetching {len(todo)}.",
        file=out,
    )

    emails: list[Email] = []
    failed = 0
    for n, message_id in enumerate(todo, start=1):
        print(f"\rFetching {n}/{len(todo)}", end="", file=err, flush=True)
        try:
            emails.append(parse_message(fetch_message(service, message_id)))
        except HttpError as exc:
            failed += 1
            status = getattr(exc.resp, "status", "?")
            print(f"\nCould not fetch {message_id}: HTTP {status}", file=err)
    if todo:
        print(file=err)

    print(file=out)
    for email in sorted(emails, key=lambda e: e.received_at, reverse=True):
        print(_format_line(email), file=out)

    print(file=out)
    summary = f"{len(emails)} candidates listed"
    if failed:
        summary += f", {failed} failed to fetch"
    print(f"{summary}. Dry run: nothing was saved.", file=out)
    return emails


def cmd_sync(args: argparse.Namespace) -> None:
    if not args.dry_run:
        print(
            "sync without --dry-run becomes available in Phase 3 (extraction). "
            "Use: python -m subtrack sync --dry-run [--limit N]",
            file=sys.stderr,
        )
        sys.exit(2)
    service = get_service(authenticate())
    run_dry_sync(service, load_processed_ids(), config.get_lookback(), args.limit)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="subtrack",
        description="Personal subscription tracker",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("auth", help="Authenticate with Gmail and print your email")

    sync = sub.add_parser("sync", help="Find candidate billing emails in Gmail")
    sync.add_argument("--dry-run", action="store_true",
                      help="List candidates (sender, subject) without saving anything")
    sync.add_argument("--limit", type=_positive_int, metavar="N",
                      help="Fetch at most N new messages")

    return parser


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the right command."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "auth": cmd_auth,
        "sync": cmd_sync,
    }
    commands[args.command](args)
