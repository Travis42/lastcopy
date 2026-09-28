"""Regression: --help must render, not crash.

Incident (2026-09-28, review pass): a literal ``%`` in the survey subparser help
string made argparse's %-formatting raise ValueError on ``--help``. The suite
tested arg parsing with real args but never exercised help rendering. Named for it.
"""

import pytest

from lastcopy.cli import build_parser


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["ingest", "--help"],
        ["enrich", "--help"],
        ["classify", "--help"],
        ["report", "--help"],
        ["survey", "--help"],
        ["confirm", "--help"],
    ],
)
def test_help_exits_cleanly(argv):
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    assert exc.value.code == 0


def test_all_help_strings_percent_safe():
    """format_help() exercises argparse %-expansion for every parser."""
    parser = build_parser()
    parser.format_help()
    for sub in parser._subparsers._group_actions[0].choices.values():
        sub.format_help()  # must not raise
