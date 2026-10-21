import pytest

from vwb.web.queue import QueueConflict, TurnQueue
from vwb.workspace import Workspace
from vwb.workspace.request import SUBDIRS, Request


def test_submission_retry_survives_restart_and_never_relaunches_cancelled_job(tmp_path):
    ws = Workspace(tmp_path / "r-retry")
    for sub in SUBDIRS:
        (ws.root / sub).mkdir(parents=True)
    ws.save(Request(id=ws.id, message="test"))
    queue = TurnQueue(tmp_path)
    try:
        queue.stop(ws)
        job = queue.enqueue(ws, message="Review item 3", idempotency_key="artifact:review:feedback")
        retry = queue.enqueue(ws, message="Review item 3", idempotency_key="artifact:review:feedback")
        assert retry["id"] == job["id"]
        assert len(queue.snapshot(ws)["queue"]) == 1
        with pytest.raises(QueueConflict, match="different message"):
            queue.enqueue(ws, message="Different item", idempotency_key="artifact:review:feedback")
        queue.cancel(ws, job["id"])
    finally:
        queue.close()
    queue = TurnQueue(tmp_path)
    try:
        retry = queue.enqueue(ws, message="Review item 3", idempotency_key="artifact:review:feedback")
        assert retry["id"] == job["id"] and retry["status"] == "cancelled"
        assert queue.snapshot(ws)["queue"] == []
    finally:
        queue.close()
