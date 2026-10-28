"""Isolated UI turn process. No vendor keys are sent to the browser."""

from __future__ import annotations

import argparse
import os
import signal
import threading
from pathlib import Path

from vwb.util.jsonio import read_json, write_json


def _watch_parent(parent_pid: int, finished: threading.Event) -> None:
    """A crashed UI must not leave an unseen paid model turn running."""
    while not finished.wait(0.5):
        if os.getppid() != parent_pid:
            os.kill(os.getpid(), signal.SIGINT)
            return


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    job = read_json(args.job)
    result_path = args.job.with_suffix(".result.json")
    finished = threading.Event()
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        if job.get("parent_pid"):
            threading.Thread(target=_watch_parent, args=(job["parent_pid"], finished), daemon=True).start()
        if job.get("lane") == "qc":
            from vwb.qc.chat import run
            from vwb.workspace import Workspace
            result = run(Workspace.open(job["parent_request"]), job["message"], cfg=job["main_config"],
                         attachment_refs=job.get("attachment_refs"), resume_message=job.get("resume_message"))
        elif job["kind"] == "compact":
            from vwb.agents.job_lead import compact
            from vwb.workspace import Workspace
            result = compact(Workspace.open(job["request"]))
        else:
            from vwb.orchestrator import chat
            result = chat(job["message"], request_id=job["request"],
                          attach=[Path(p) for p in job.get("attachments", [])],
                          attachment_refs=job.get("attachment_refs"),
                          skip_index=job.get("skip_index", True), context=job.get("context"),
                          main_config=job.get("main_config"), resume_message=job.get("resume_message"))
        write_json(result_path, {"status": "completed", "result": result})
        return 0
    except KeyboardInterrupt:
        write_json(result_path, {"status": "stopped"})
        return 130
    except BaseException as exc:
        write_json(result_path, {"status": "failed", "error": str(exc)[:2000]})
        return 1
    finally:
        finished.set()


if __name__ == "__main__":
    raise SystemExit(main())
