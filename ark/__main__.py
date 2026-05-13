"""Entry point: `python -m ark <command>`. See `python -m ark --help`."""

from __future__ import annotations

from . import cli


def main(argv: list[str] | None = None) -> int:
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
