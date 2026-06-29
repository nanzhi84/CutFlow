from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# OpenAPI export imports the app only to build schema. Local proxy env can make
# httpx initialize optional SOCKS support during provider registration.
for _name in (
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
):
    os.environ.pop(_name, None)

# Building the app now requires a database URL (the in-memory backend was removed),
# but OpenAPI export never opens a connection. Provide a placeholder so the export
# works without a live database; a real CUTAGENT_DATABASE_URL (CI) takes precedence.
os.environ.setdefault("CUTAGENT_STORAGE_BACKEND", "sqlalchemy")
os.environ.setdefault(
    "CUTAGENT_DATABASE_URL", "postgresql+psycopg://openapi:openapi@127.0.0.1:5432/openapi"
)

from apps.api.main import app


def main() -> None:
    output = Path("apps/web/src/api/openapi.json")
    output.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
