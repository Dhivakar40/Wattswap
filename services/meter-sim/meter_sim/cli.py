"""Command-line entrypoint. Signed-reading generation is implemented in Phase 2."""

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="meter-sim",
        description="WattSwap smart-meter SIMULATOR (labeled simulation, not real hardware).",
    )


def main(argv: Sequence[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
