"""Human QC routes. Private state is never included in the Edit snapshot."""
from __future__ import annotations

import uuid
from copy import deepcopy
from urllib.parse import quote

from filelock import FileLock

from vwb import models
from vwb.agents.events import context_usage, event_page
from vwb.config import slot
from vwb.qc.chat import QCWorkspace, configuration, rendered_path
from vwb.qc.chat_store import QCStore
from vwb.util.hashing import sha256_json
from vwb.util.jsonio import read_json, write_json
from vwb.workspace import attachments


def workspace(parent):
    ws = QCWorkspace(parent)
    ws.initialize()
    return ws


def attachment_entry(parent, ref):
    return {**ref, "url": f"/api/requests/{quote(parent.id)}/qc/attachments/{ref['id']}"}


def snapshot(app, parent):
    ws = workspace(parent)
    cfg = slot("qc")
    request = ws.load().model_dump(mode="json")
    for turn in request["chat"]:
        turn["attachments"] = [attachment_entry(parent, ref) for ref in turn.get("attachments", [])]
    store = QCStore(parent)
    store.reconcile()
    targets = [{**row, "url": app.file_url(parent, rendered_path(parent, row["target"]))}
               for row in store.targets()]
    queue = app.queue.snapshot(ws)
    for job in [*queue["queue"], *([queue["active"]] if queue["active"] else [])]:
        job["attachment_refs"] = [attachment_entry(parent, ref) for ref in job.get("attachment_refs", [])]
    return {"request": request, **queue, **event_page(ws), "usage": context_usage(ws),
            "model": {"model": cfg["model"], "provider": cfg["provider"]},
            "targets": targets, "items": store.list_items(audience="qc"),
            "rechecks": [{**record, "job": app.queue.recheck_job(ws, record["id"])}
                         for record in store.list_rechecks()]}


def submit_recheck(app, parent, ws, store, body):
    from vwb.models import image_input_supported
    from vwb.qc.recheck import INSTRUCTIONS, independent, targets

    required = {"item_id", "target", "f0", "f1", "pin", "expected_revision", "idempotency_key"}
    if not isinstance(body, dict) or set(body) != required:
        raise ValueError("Choose a finding, pinned newer version and frame window for the recheck.")
    key = body["idempotency_key"]
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ValueError("Invalid recheck submission key.")
    signature = sha256_json(body)
    path = ws.memory / "recheck_submissions.json"
    with FileLock(str(path.with_suffix(".lock"))):
        submissions = read_json(path, {})
        previous = submissions.get(key)
        if previous is not None and previous["signature"] != signature:
            raise ValueError("This recheck submission key belongs to another selection.")
        if previous is None:
            cfg = deepcopy(slot("qc"))
            if cfg.get("provider") not in {"anthropic", "openrouter"} or not image_input_supported(cfg):
                raise ValueError("Rechecks require an Anthropic or OpenRouter model with image input.")
            cfg["host"] = f"{cfg['provider']}_api"
            cfg.pop("fallbacks", None)
            cfg["qc_recheck_instructions"] = INSTRUCTIONS
            cfg["max_turns"] = min(int(cfg.get("max_turns", 12)), 12)
            cfg["max_output_tokens"] = min(int(cfg.get("max_output_tokens", 8192)), 8192)
            finding = store.get(body["item_id"])
            for target in (finding["anchor"], store.verify_target(body["target"])):
                independent(target, cfg["model"])
            # Persist model/prompt BEFORE preparation. A retry after any later
            # crash must retain this configuration and the same durable job key.
            previous = {"signature": signature, "cfg": cfg}
            submissions[key] = previous
            write_json(path, submissions)
        cfg = deepcopy(previous["cfg"])
        record = store.prepare_recheck(body["item_id"], body["target"], f0=body["f0"], f1=body["f1"],
                                       pin=body["pin"], expected_revision=body["expected_revision"],
                                       idempotency_key=key, model=cfg["model"], provider=cfg["provider"])
        cfg.update(qc_recheck_id=record["id"], qc_targets=targets(record))
        job = app.queue.enqueue(ws, message=f"Recheck finding {record['item_id']} on {record['after']['target']}.",
                                main_config=cfg, skip_index=True, idempotency_key=f"qc-recheck:{record['id']}")
        return {"recheck": record, "job": job}


def mutate(app, parent, path, body):
    ws = workspace(parent)
    store = QCStore(parent)
    if path == ["rechecks"]:
        return submit_recheck(app, parent, ws, store, body)
    if path == ["messages"]:
        message = app.message(body)
        mode = body.get("mode", "queue")
        if mode not in {"queue", "now"}:
            raise ValueError("Choose queue or now for the QC message.")
        key = body.get("idempotency_key", uuid.uuid4().hex)
        if not isinstance(key, str) or not 1 <= len(key) <= 240:
            raise ValueError("Invalid QC submission key.")
        uploads = body.get("attachments", [])
        signature = sha256_json({"message": message, "mode": mode, "uploads": uploads})
        path = ws.memory / "submissions.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(path.with_suffix(".lock"))):
            submissions = read_json(path, {})
            previous = submissions.get(key)
            if previous is not None and previous["signature"] != signature:
                raise ValueError("This QC submission key belongs to another message.")
            if previous is None:
                cfg = configuration(parent)
                refs = attachments.ingest(ws, app.attachment_paths(uploads))
                previous = {"signature": signature, "refs": refs, "cfg": cfg}
                submissions[key] = previous
                write_json(path, submissions)
            job = app.queue.enqueue(ws, message=message, mode=mode, main_config=previous["cfg"],
                                    attachment_refs=previous["refs"], skip_index=True, idempotency_key=key)
        return {"job": job}
    if path == ["stop"]:
        return app.queue.stop(ws)
    if path == ["resume"]:
        return app.queue.resume(ws, job_id=body.get("job_id"))
    if path == ["items"]:
        if not isinstance(body.get("pin"), dict):
            raise ValueError("Refresh the reviewed video before creating a finding; a media pin is required.")
        return {"item": store.create(body, actor="human")}
    if len(path) == 3 and path[0] == "items" and path[2] == "status":
        if set(body) - {"status", "expected_revision", "note"}:
            raise ValueError("Unexpected finding status fields.")
        return {"item": store.transition(path[1], body.get("status"), actor="human",
                                          expected_revision=body.get("expected_revision"), note=body.get("note", ""))}
    if path == ["handoff"]:
        def enqueue(**kwargs):
            return app.queue.enqueue(parent, skip_index=True,
                                     main_config=models.explicit_runtime(parent, app.runs_dir), **kwargs)
        return {"batch": store.handoff(body.get("item_ids"), enqueue,
                                        idempotency_key=body.get("idempotency_key"),
                                        expected_revisions=body.get("expected_revisions"))}
    raise FileNotFoundError("That QC action was not found.")
