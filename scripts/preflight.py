"""Production startup preflight CLI (issue #66).

Builds a Settings snapshot from the current environment and reports any
production-unsafe configuration. Intended to run before a production deploy:

    CUTAGENT_ENV=production python scripts/preflight.py
    CUTAGENT_ENV=production python scripts/preflight.py --json

Exit code is non-zero when unsafe settings are found (and in production, the
API/worker themselves fail closed on the same checks at startup).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.core.config import (  # noqa: E402
    build_settings,
    format_preflight_report,
    validate_startup_settings,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Cutagent production startup preflight.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable JSON report instead of the human report.",
    )
    args = parser.parse_args()

    settings = build_settings()
    issues = validate_startup_settings(settings)

    if args.json:
        print(
            json.dumps(
                {
                    "environment": settings.deployment.environment,
                    "ok": not issues,
                    "issue_count": len(issues),
                    "issues": issues,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(format_preflight_report(issues))
        if settings.deployment.environment != "production":
            print(
                f"(environment={settings.deployment.environment!r}; "
                "production-only checks are skipped — set CUTAGENT_ENV=production to enforce.)"
            )

    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
