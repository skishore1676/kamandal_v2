"""Lazy console entrypoint that keeps read-only status free of live imports."""

from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int | None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "experiment-status":
        from kamandal_v2.experiment_status import main as status_main

        return status_main(arguments[1:])

    # Preserve the established CLI and its argument parsing for every other
    # command.  It is imported only after the status route has been ruled out.
    from kamandal_v2.cli import main as legacy_main

    return legacy_main()


if __name__ == "__main__":
    raise SystemExit(main())
