"""CLI entry point for subtrack."""

import argparse
import sys

from subtrack.gmail_client import authenticate, get_service, get_profile


def cmd_auth(args: argparse.Namespace) -> None:
    """Run the OAuth flow and print the authenticated Gmail address."""
    creds = authenticate()
    service = get_service(creds)
    email = get_profile(service)
    print(email)


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="subtrack",
        description="Personal subscription tracker",
    )
    sub = parser.add_subparsers(dest="command")

    # auth
    sub.add_parser("auth", help="Authenticate with Gmail and print your email")

    return parser


def main() -> None:
    """Parse arguments and dispatch to the right command."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    commands = {
        "auth": cmd_auth,
    }
    commands[args.command](args)
