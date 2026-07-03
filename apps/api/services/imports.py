from __future__ import annotations

from fastapi import Request

from apps.api.common import (
    production_repository,
    request_id,
)
from packages.core import contracts as c
from packages.core.workflow import NodeExecutionError

def import_batch(payload: c.CreateImportBatchRequest, request: Request) -> c.ImportBatchReport:
    repo = production_repository(request)
    if repo is None:
        raise NodeExecutionError(c.ErrorCode.import_failed, "Import repository is unavailable.")
    report = repo.create_import_batch(payload, request_id())
    if report is None:
        raise NodeExecutionError(c.ErrorCode.import_failed, "Import type is not supported.")
    return report


def import_batch_detail(request: Request, batch_id: str) -> c.ImportBatchReport:
    repo = production_repository(request)
    if repo is None:
        raise NodeExecutionError(c.ErrorCode.import_failed, "Import repository is unavailable.")
    report = repo.get_import_batch(batch_id)
    if report is None:
        raise NodeExecutionError(c.ErrorCode.import_failed, "Import batch is not available.")
    return report
