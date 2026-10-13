"""Unit tests for the harness's pure logic (no models, no media)."""

from __future__ import annotations

import json

import pytest

from vwb.canvas.anim import ease, value_at
from vwb.canvas.compile import caption_pages, plan
from vwb.canvas.copydeck import mark_superscripts, plain
from vwb.canvas.model import Canvas
from vwb.index import asr
from vwb.reframe import camera
from vwb.tools.base import Registry
from vwb.workspace import flags
from vwb.workspace.ledger import Ledger


# ---------------------------------------------------------------- keyframes

def test_value_at_static_and_hold():
    assert value_at([1, 2, 3, 4], 10) == [1, 2, 3, 4]
    keys = [{"f": 0, "v": [0, 0, 10, 10]}, {"f": 10, "v": [10, 0, 10, 10], "ease": "hold"}]
    assert value_at(keys, 5) == [0, 0, 10, 10]          # hold keeps the previous value until the key
    assert value_at(keys, 10) == [10, 0, 10, 10]
    assert value_at(keys, 99) == [10, 0, 10, 10]


def test_value_at_eases_are_monotonic_and_bounded():
    keys = [{"f": 0, "v": 0.0}, {"f": 100, "v": 1.0, "ease": "inout"}]
    vals = [value_at(keys, f) for f in range(0, 101)]
    assert vals[0] == 0.0 and abs(vals[-1] - 1.0) < 1e-9
    assert all(b >= a for a, b in zip(vals, vals[1:]))
    for kind in ("linear", "in", "out", "inout"):
        assert ease(0, kind) == 0 and abs(ease(1, kind) - 1) < 1e-9


# ---------------------------------------------------------------- ledger and flags

def test_ledger_chain_detects_tampering(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    for i in range(5):
        led.append("note", "test", {"i": i})
    assert led.verify_chain() == (True, None)
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    row = json.loads(lines[2])
    row["data"]["i"] = 99
    lines[2] = json.dumps(row)
    (tmp_path / "ledger.jsonl").write_text("\n".join(lines) + "\n")
    ok, bad = led.verify_chain()
    assert not ok and bad == 3


def test_severity_bumps_and_caps():
    assert flags.severity("UPSCALED") == "WARN"
    assert flags.severity("UPSCALED", bumps=["regulated"]) == "HOLD"
    assert flags.severity("NEEDS-SOURCE:logo", bumps=["mark"]) == "HOLD"       # capped at its max
    assert flags.severity("SOURCE-TERMS") == "BLOCK"
    with pytest.raises(KeyError):
        flags.severity("NOT-A-KIND")


def test_text_unverified_table():
    assert flags.text_unverified_severity(readers_agree=True, regulated=True) == "HOLD"
    assert flags.text_unverified_severity(readers_agree=False, regulated=True) == "BLOCK"
    assert flags.text_unverified_severity(readers_agree=False, regulated=False) == "WARN"


def test_flag_state_replay_and_triage(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    f1 = flags.make("UPSCALED", "1.4x", output="o", version=1, subject="up")
    f2 = flags.make("CHECK-UNQUALIFIED", "v0", output="o", version=1, subject="unq")
    for f in (f1, f2):
        led.append("flag.raised", "verifier", f.to_dict())
    cur = flags.current_flags(led, output="o", version=1)
    assert flags.triage(cur, attested=True, checks_qualified=False) == "YELLOW"
    led.append("human.ack", "human:x", {"id": f1.id}, signed=True)
    cur = {f["id"]: f for f in flags.current_flags(led, output="o")}
    assert cur[f1.id]["status"] == "acknowledged"
    assert flags.triage(list(cur.values()), attested=False, checks_qualified=True) == "RED"


# ---------------------------------------------------------------- transcript consensus

def _w(text, t0, t1, conf=0.99):
    return {"text": text, "t0": t0, "t1": t1, "conf": conf}


def test_consensus_drops_zero_duration_insertions():
    w1 = [_w("While", 0.0, 0.3), _w("disease", 0.3, 0.6), _w("is", 0.6, 0.6, 0.2), _w("a", 0.6, 0.6, 0.1),
          _w("risk,", 0.6, 0.6, 0.1), _w("can", 0.6, 0.9), _w("be", 0.9, 1.1), _w("severe.", 1.1, 1.5)]
    out = asr.consensus(w1, "While disease can be severe.")
    assert " ".join(w["text"] for w in out) == "While disease can be severe."
    assert all(w["agree"] for w in out)


def test_consensus_prefers_reading_that_screen_supports():
    w1 = [_w("Adults", 0.0, 0.4), _w("52-64", 0.4, 1.0, 0.95), _w("have", 1.0, 1.2)]
    out = asr.consensus(w1, "Adults 50 to 64 have", screen_text="Adults aged 50–64 are at risk")
    assert " ".join(w["text"] for w in out) == "Adults 50 to 64 have"
    assert [w["agree"] for w in out] == [1, 0, 0, 0, 1]


def test_consensus_treats_hyphen_splits_as_agreement():
    w1 = [_w("up", 0.0, 0.2), _w("to", 0.2, 0.3), _w("date.", 0.3, 0.6)]
    out = asr.consensus(w1, "up-to-date.")
    assert [w["text"] for w in out] == ["up-to-date."] and out[0]["agree"] == 1


def test_number_normalisation():
    assert asr.normalize_tokens("fifty two percent") == ["52", "%"]
    assert asr.normalize_tokens("aged 50–64") == ["aged", "50", "to", "64"]


# ---------------------------------------------------------------- copy deck

def test_superscript_markers():
    assert mark_superscripts("adults ≥50 years1,2") == "adults ≥50 years^{1,2}"
    assert mark_superscripts("in 2019.*") == "in 2019.*" or mark_superscripts("in 2019.*").endswith("*")
    assert plain("years^{1,2}") == "years1,2"
    assert mark_superscripts("aged 50-64") == "aged 50-64"          # ranges are not references


# ---------------------------------------------------------------- camera

def test_best_window_avoids_cutting_lines_when_it_can():
    p = camera.CameraParams(aspect=1.5, out_w=1080, margin=0.04, max_scale=2.4, safe_left=0.03, safe_right=0.97)
    focus = (1110, 355, 730, 150)                   # a text panel
    above = (76, 279, 1781, 36)                     # a full-width subtitle just above it
    below = (282, 924, 1357, 122)                   # footnotes below
    win = camera.best_window(focus, [above, below], 1920, 1080, p)
    x, y, w, h = win
    assert abs(w / h - 1.5) < 1e-6
    inside = lambda b: x <= b[0] and y <= b[1] and b[0] + b[2] <= x + w and b[1] + b[3] <= y + h
    assert inside(focus)
    for b in (above, below):
        ix = max(0, min(b[0] + b[2], x + w) - max(b[0], x))
        iy = max(0, min(b[1] + b[3], y + h) - max(b[1], y))
        frac = ix * iy / (b[2] * b[3])
        assert frac < 0.04 or frac > 0.96


def test_camera_plan_is_monotonic_and_cuts_on_shot_change():
    p = camera.CameraParams(aspect=1.5, out_w=1080)
    beats = [{"o0": 0, "o1": 90, "shot": 1, "focus_box": (100, 100, 400, 200)},
             {"o0": 90, "o1": 180, "shot": 1, "focus_box": (1200, 600, 400, 200)},
             {"o0": 180, "o1": 270, "shot": 2, "focus_box": (600, 300, 600, 300)}]
    keys = camera.plan(beats, 1920, 1080, 30, p)
    fs = [k.f for k in keys]
    assert fs == sorted(fs) and len(set(fs)) == len(fs)
    assert any(k.f == 180 and k.ease == "hold" for k in keys)       # a source cut is a camera cut


# ---------------------------------------------------------------- compile

def test_caption_pages_break_on_sentences_and_length():
    words = [{"text": t, "t0": i * 0.3, "t1": i * 0.3 + 0.25} for i, t in
             enumerate("This is the first sentence. And this one is a much longer second sentence here.".split())]
    pages = caption_pages(words, max_chars=24, max_lines=2)
    flat = [[w["text"] for ln in pg for w in ln] for pg in pages]
    assert flat[0][-1] == "sentence."                                # a page ends at a sentence end
    assert all(sum(len(w) + 1 for w in ln) <= 25 for pg in pages for ln in [[w["text"] for w in l] for l in pg])


def test_plan_groups_text_layers_between_video_layers():
    c = Canvas.model_validate({"schema": "vwb.canvas/2", "request": "r", "output": "o",
                               "format": {"width": 1080, "height": 1920, "fps": 30},
                               "time_map": [{"media": "src1@x", "src_in": 0, "src_out": 30}], "copy": [],
                               "layers": [
                                   {"id": "bg", "type": "video", "source": "src1@x", "box": [0, 0, 1080, 1920]},
                                   {"id": "s", "type": "shape", "shape": "rect", "box": [0, 0, 10, 10]},
                                   {"id": "cap", "type": "captions", "box": [60, 1400, 900, 120], "pages": [],
                                    "style": {"font": "Inter", "weight": 700, "size": 48, "color": "#FFFFFF"}},
                                   {"id": "pip", "type": "video", "source": "src1@x", "box": [600, 1200, 300, 300]},
                                   {"id": "fx", "type": "clip", "asset": "slot:x@1", "box": [0, 0, 100, 100]},
                                   {"id": "bar", "type": "shape", "shape": "path", "d": "M0 0 L10 0",
                                    "box": [0, 1900, 1080, 6]}]})
    steps = plan(c)
    assert [s.kind for s in steps] == ["video", "plate", "video", "clip", "plate"]
    assert [l.id for l in steps[1].layers] == ["s", "cap"]
    assert steps[1].plate_id != steps[4].plate_id


# ---------------------------------------------------------------- policy

def test_role_allowlists():
    assert Registry.allowed_for_role("job_lead", "canvas_commit")
    assert not Registry.allowed_for_role("composer", "canvas_commit")     # only the job lead commits
    assert not Registry.allowed_for_role("critic", "file_write")
    assert Registry.allowed_for_role("composer", "file_edit")
    assert not Registry.allowed_for_role("unknown_role", "index_overview")


def test_schema_has_no_house_style():
    from pydantic import ValidationError

    from vwb.canvas.model import TextLayer, font_library
    with pytest.raises(ValidationError):                      # a text layer must choose its own type
        TextLayer.model_validate({"id": "t", "copy_id": "c", "box": [0, 0, 10, 10]})
    t = TextLayer.model_validate({"id": "t", "copy_id": "c", "box": [0, 0, 10, 10],
                                  "style": {"font": "Arimo", "weight": 700, "size": 40, "color": "#112233"}})
    assert t.animate.enter == "none" and t.style.shadow is False and t.style.accent_bar is None
    lib = font_library()
    assert len(lib) >= 10 and "Arimo" in lib and 700 in lib["Arimo"]["weights"]


def test_colour_keyframes_interpolate():
    assert value_at([{"f": 0, "v": "#000000"}, {"f": 10, "v": "#FFFFFF", "ease": "linear"}], 5) == "#808080FF"


# ---------------------------------------------------------------- generative toolkit (pure parts)

def test_valid_model_sizes():
    from vwb.gen.images import MAX_EDGE, MAX_PX, MIN_PX, valid_size
    for w, h in [(1080, 1920), (1920, 1080), (300, 200), (5000, 5000), (100, 900), (1536, 1024)]:
        W, H = valid_size(w, h)
        assert W % 16 == 0 and H % 16 == 0 and MIN_PX <= W * H <= MAX_PX and max(W, H) <= MAX_EDGE
        assert 1 / 3 - 1e-3 <= W / H <= 3 + 1e-3


def test_key_choice_avoids_subject_colours_and_keys_cleanly():
    import numpy as np

    from vwb.gen import key
    green_subject = np.zeros((64, 64, 4), np.uint8)
    green_subject[..., :3] = (20, 170, 60)
    green_subject[..., 3] = 255
    name, rgb, risk = key.choose_key([green_subject])
    assert name != "green" and risk["green"] > 0.5
    frame = np.zeros((200, 120, 3), np.uint8)
    frame[:] = (0, 177, 64)
    frame[80:120, 40:80] = (200, 40, 40)                       # a red subject on the key
    bg, noise = key.background_key(frame)
    rgba = key.key_frame(frame, bg, noise)
    assert rgba[100, 60, 3] == 255 and rgba[10, 10, 3] == 0
    assert key.matte_metrics(rgba)["holes"] == 0.0


def test_outpaint_keeps_placed_pixels_exact():
    import numpy as np

    from vwb.gen import qa
    orig = np.random.default_rng(1).integers(0, 255, (60, 80, 3), dtype=np.uint8)
    keep = np.zeros((60, 80), np.float32)
    keep[10:50, 20:60] = 1
    gen = orig.copy()
    assert qa.seam(gen, orig, keep)["mae"] == 0.0                  # a model that kept the content in place
    shifted = np.roll(orig, 3, axis=1)
    assert qa.seam(shifted, orig, keep)["mae"] > 10                 # one that moved it shows a seam


def test_compact_json_and_image_bounds():
    import io
    import json as js

    import numpy as np
    from PIL import Image

    from vwb.media.frames import fit_image
    from vwb.util.jsonio import dumps_compact
    obj = {"rows": [{"id": i, "t": "x"} for i in range(3)], "deep": {"a": [1, 2], "b": "y" * 300}}
    assert js.loads(dumps_compact(obj)) == obj and dumps_compact(obj).count("\n") < 12
    big = np.random.default_rng(0).integers(0, 255, (2400, 3200, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(big).save(buf, format="PNG")
    small = fit_image(buf.getvalue())
    im = Image.open(io.BytesIO(small))
    assert max(im.size) <= 1568 and len(small) <= 600_000


def test_difference_key_has_no_colour_fringe():
    import cv2
    import numpy as np

    from vwb.gen.images import _difference_key
    bg = np.array([20, 40, 200], np.float32)                  # a saturated blue background
    img = np.zeros((120, 120, 3), np.uint8)
    img[:] = bg.astype(np.uint8)
    cv2.circle(img, (60, 60), 35, (250, 250, 250), -1, lineType=cv2.LINE_AA)   # white subject, soft edge
    alpha, fg = _difference_key(img, bg)
    edge = (alpha > 0.05) & (alpha < 0.95)
    assert edge.any() and alpha[60, 60] == 1.0 and alpha[2, 2] == 0.0
    # edge colours stay on the subject's colour, never the complement of the background (yellow)
    assert (fg[edge][:, 2] > 200).all()


# ---------------------------------------------------------------- remake toolkit (pure parts)

def test_shape_kit_geometry():
    from vwb.canvas.model import ShapeLayer
    from vwb.canvas.shapekit import iso_bars, pictogram, ring
    bars = iso_bars("b", [100, 1000, 800, 500], [100, 200, 400], max_value=400, start=0, stagger=10, grow=20)
    assert len(bars) == 3 and all(len(b["paths"]) == 3 for b in bars)
    finals = [b["box"][-1]["v"] for b in bars]
    assert finals[2][3] > finals[1][3] > finals[0][3]                       # taller for larger values
    assert all(abs((f[1] + f[3]) - 1500) < 1 for f in finals)               # every bar stands on the baseline
    for layer in bars + ring("r", [0, 0, 300, 300], 0.25) + pictogram("p", [0, 0, 500, 100], 2, 10, 7):
        ShapeLayer.model_validate(layer)                                     # valid canvas layers


def test_reader3_settles_two_of_three():
    from vwb.index.reader3 import settle
    assert settle("1. Kang N, et al. Pathogens. 2025;14:41.", "1. Kang N, et aI. Pathogens. 2025;14:4l.",
                  "1. Kang N, et al. Pathogens. 2025;14:41.") == ("1. Kang N, et al. Pathogens. 2025;14:41.", True)
    assert settle("years1,2", "yearsl,2", "years^{1,2}")[1] is True             # superscript markers fold away
    assert settle("abc", "xyz", "pqr") == ("abc", False)


def test_usage_normalisation_across_vendors():
    from vwb.workspace.spend import _tokens
    assert _tokens({"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 3},
                    "input_tokens_details": {"cached_tokens": 4}}) == {"input": 10, "output": 5, "reasoning": 3, "cached": 4}
    assert _tokens({"prompt_token_count": 7, "candidates_token_count": 2, "thoughts_token_count": 1,
                    "cached_content_token_count": 6}) == {"input": 7, "output": 2, "reasoning": 1, "cached": 6}


def test_srt_timestamps():
    from vwb.verify.sidecars import _ts
    assert _ts(0) == "00:00:00,000" and _ts(61.2345) == "00:01:01,234" and _ts(3600.5) == "01:00:00,500"


def test_person_only_keeps_face_anchored_regions():
    import numpy as np

    from vwb.gen.matting import _person_only
    a = np.zeros((200, 300), np.uint8)
    a[60:200, 120:180] = 255           # a person under the face
    a[10:30, 10:40] = 255              # a logo elsewhere
    out = _person_only(a, [(250, 140, 60, 60)], 0.5)    # face box in source pixels (2x)
    assert out[100, 150] > 200 and out[20, 20] == 0


# ---------------------------------------------------------------- the main agent's conversation

def test_missed_turns_only_when_another_host_replied():
    from types import SimpleNamespace as T

    from vwb.agents.job_lead import missed_turns
    chat = [T(role="human", text="make shorts"), T(role="job_lead", text="r1"),
            T(role="human", text="bigger title"), T(role="job_lead", text="r2 (fallback host)"),
            T(role="human", text="thanks, now 45 s")]
    assert [t.text for t in missed_turns(chat, 1)] == ["bigger title", "r2 (fallback host)"]
    assert missed_turns(chat, 2) == []                  # the thread saw every reply
    assert missed_turns(chat[:3] + [T(role="human", text="again")], 1) == []   # unanswered (failed) turns only
    assert [t.text for t in missed_turns(chat, 0)][-1] == "r2 (fallback host)"


def test_human_events_since_the_last_message():
    from vwb.agents.job_lead import human_events
    rows = [{"seq": 3, "kind": "human.ack", "actor": "human:ana", "data": {"id": "F-1"}},
            {"seq": 7, "kind": "human.answer", "actor": "human:ana", "data": {"ticket": "Q-2", "option": "b", "text": ""}},
            {"seq": 8, "kind": "human.waive", "actor": "human:raj", "data": {"id": "F-3", "reason": "legal ok"}},
            {"seq": 9, "kind": "human.accept", "actor": "human:raj", "output": "short-01", "version": 2, "data": {}}]
    assert human_events(rows, 5) == ["ana answered Q-2: b", "raj waived flag F-3: legal ok", "raj accepted short-01 v2"]
    assert human_events(rows, 9) == []


def test_turn_recovery_branches():
    from vwb.agents.host_codex import HostUnavailable, ThreadMissing
    from vwb.agents.job_lead import MAX_FAILED_TURNS, _with_resume

    class FakeLedger:
        def __init__(self):
            self.rows = []

        def append(self, kind, actor, data=None, **kw):
            self.rows.append((kind, (data or {}).get("action")))

    def scripted(*outcomes):
        calls = []

        def turn(text, thread):
            calls.append((text[:12], thread))
            out = outcomes[len(calls) - 1]
            if isinstance(out, BaseException):
                raise out
            return out
        return turn, calls

    # a turn that dies part-way resumes the same thread once
    led, rec = FakeLedger(), {"thread_id": "T1"}
    turn, calls = scripted(RuntimeError("container gone"), "done")
    assert _with_resume(turn, "make it", rec, led) == "done"
    assert [c[1] for c in calls] == ["T1", "T1"] and calls[1][0].startswith("[harness]")
    assert rec == {"thread_id": "T1"} and ("host.error", "resume") in led.rows

    # a host that cannot start hands the turn on at once and keeps the thread
    led, rec = FakeLedger(), {"thread_id": "T1"}
    turn, calls = scripted(HostUnavailable("docker down"))
    with pytest.raises(HostUnavailable):
        _with_resume(turn, "make it", rec, led)
    assert len(calls) == 1 and rec == {"thread_id": "T1"}

    # a thread gone from the store restarts from the saved state, without retrying the resume
    led, rec = FakeLedger(), {"thread_id": "T1"}
    turn, calls = scripted(ThreadMissing("no rollout"), "fresh reply")
    assert _with_resume(turn, "make it", rec, led, fresh=lambda: "WAKE: state + make it") == "fresh reply"
    assert calls == [("make it", "T1"), ("WAKE: state ", None)] and ("host.error", "new_thread") in led.rows

    # a thread is dropped only after MAX_FAILED_TURNS failed turns in a row; a success resets the count
    led, rec = FakeLedger(), {"thread_id": "T1"}
    for n in range(1, MAX_FAILED_TURNS + 1):
        turn, _ = scripted(RuntimeError("api 500"), RuntimeError("api 500"))
        with pytest.raises(RuntimeError):
            _with_resume(turn, "make it", rec, led)
        assert bool(rec) == (n < MAX_FAILED_TURNS)
    assert ("host.error", "drop_thread") in led.rows
    rec = {"thread_id": "T1", "failures": 1}
    turn, _ = scripted("ok")
    assert _with_resume(turn, "hi", rec, FakeLedger()) == "ok" and rec == {"thread_id": "T1"}


def test_sheen_sweeps_once_inside_its_window():
    import numpy as np

    from vwb.canvas.model import Sheen
    from vwb.render.compositor import apply_sheen
    rgb = np.zeros((60, 120, 3), np.uint8)
    rgb[:] = (20, 40, 160)
    s = Sheen(start=10, frames=20, angle=0, width=0.2, strength=0.8)
    assert (apply_sheen(rgb, s, 5) == rgb).all() and (apply_sheen(rgb, s, 30) == rgb).all()    # before and after
    mid = apply_sheen(rgb, s, 20)
    col = mid[30].astype(int).sum(axis=1)
    assert col.max() > rgb[0, 0].astype(int).sum() + 200            # a bright band crosses the middle
    assert abs(int(np.argmax(col)) - 60) < 15                          # ... near the centre at mid-sweep
    early = apply_sheen(rgb, s, 12)[30].astype(int).sum(axis=1)
    assert int(np.argmax(early)) < int(np.argmax(col))                 # and it travels left to right
    again = Sheen(start=0, frames=10, repeat=40, angle=0, width=0.2)
    assert (apply_sheen(rgb, again, 45) != rgb).any() and (apply_sheen(rgb, again, 25) == rgb).all()


def test_matte_erosion_pulls_the_edge_in():
    import cv2
    import numpy as np

    from vwb.gen.key import refine_alpha
    a = np.zeros((80, 80), np.float32)
    cv2.circle(a, (40, 40), 25, 1.0, -1)
    eroded = refine_alpha(a, erode=3)
    assert eroded.sum() < a.sum() and eroded[40, 40] == 1.0 and eroded[40, 14] == 0.0    # edge pulled in by ~3 px
    soft = refine_alpha(a, erode=2, feather=1.5)
    assert ((soft > 0.02) & (soft < 0.98)).sum() > 0 and (soft <= refine_alpha(a, erode=2) + 1e-6).all()


def test_resumed_thread_hears_what_changed_in_the_harness():
    from vwb.agents.job_lead import surface_note
    old = {"tools": {"a": "1", "b": "2", "gone": "3"}, "docs": {"instructions": "i1", "canvas_schema": "c1", "recipe:remake": "r1"}}
    new = {"tools": {"a": "1", "b": "9", "c": "4"}, "docs": {"instructions": "i1", "canvas_schema": "c2", "recipe:remake": "r1"}}
    note = surface_note(old, new)
    assert "new tools: c" in note and "changed tools" in note and "b" in note and "removed tools: gone" in note
    assert "canvas_schema" in note and "skill_read remake" not in note and "instructions were updated" not in note
    assert surface_note(old, old) == ""
    swapped = dict(new, docs=dict(new["docs"], instructions="i2"))
    assert "Your instructions were updated" in surface_note(old, swapped)


def test_key_is_not_measured_on_a_subject_that_fills_the_border():
    import numpy as np

    from vwb.gen import key
    frame = np.zeros((200, 120, 3), np.uint8)
    frame[:] = (0, 177, 64)                               # the requested green
    frame[:, :] = (40, 90, 220)                           # ... but a blue subject now fills the whole border
    frame[60:140, 30:90] = (0, 170, 70)                   # and the only green is inside
    k, _ = key.background_key(frame, expected=(0, 177, 64))
    assert abs(float(k[1]) - 177) < 10 and float(k[2]) < 100             # the key stays green, not blue
    blind, _ = key.background_key(frame)                                  # without the hint it would take the blue
    assert float(blind[2]) > 200
    rgba = key.key_frame(frame, k, 1.0)
    assert rgba[5, 5, 3] == 255 and rgba[100, 60, 3] == 0                # the blue subject survives the key
