"""Speech to words with two independent readers and a word-level consensus.

Reader 1 is Whisper large-v3-turbo on MLX (word timestamps). Its prompt carries a glossary
mined from on-screen text, which helps with drug and disease names. Reader 2 is Gemini on
the audio track only, so it cannot copy the slides. Disagreements are kept, not hidden:
each word records whether the readers agreed and what the other reader heard (spec D21).
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any

from vwb.gateway import Gateway
from vwb.gateway import gemini as gem
from vwb.util.log import log

_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000}
_STOP = set("a an the of and or to in on for with by at as is are was were be been it its this that these those "
            "from than then so such can may also into over under per about".split())


def _words_to_numbers(tokens: list[str]) -> list[str]:
    """Collapse number words into digits: ['fifty', 'two'] -> ['52'] (simple English forms)."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _ONES or tok in _TENS:
            total, current = 0, 0
            j = i
            while j < len(tokens) and (tokens[j] in _ONES or tokens[j] in _TENS or tokens[j] in _SCALES
                                       or (tokens[j] == "and" and j + 1 < len(tokens) and tokens[j + 1] in _ONES | _TENS)):
                t = tokens[j]
                if t in _ONES:
                    current += _ONES[t]
                elif t in _TENS:
                    current += _TENS[t]
                elif t in _SCALES:
                    scale = _SCALES[t]
                    if scale == 100:
                        current = max(current, 1) * 100
                    else:
                        total += max(current, 1) * scale
                        current = 0
                j += 1
            out.append(str(total + current))
            i = j
            continue
        out.append(tok)
        i += 1
    return out


def normalize_tokens(text: str) -> list[str]:
    """Comparison form: lowercase, NFKC, hyphens split, number words to digits, % kept."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("≥", " >= ").replace("≤", " <= ").replace("%", " % ")
    text = re.sub(r"(?<=\d)\s*[-–—]\s*(?=\d)", " to ", text)          # 50-64 -> 50 to 64
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)                     # 1,000 -> 1000
    text = text.replace("percent", " % ").replace("per cent", " % ")
    text = re.sub(r"[‐-‒–—-]", " ", text)
    text = re.sub(r"[^\w%.>=<]+", " ", text)
    tokens = [t.strip(".") for t in text.split() if t.strip(".")]
    return _words_to_numbers(tokens)


def norm_word(text: str) -> str:
    toks = normalize_tokens(text)
    return " ".join(toks)


# ---------------------------------------------------------------- reader 1 (Whisper on MLX)

def glossary_from_text(texts: list[str], limit: int = 60) -> str:
    """Rare, long words from on-screen text, used as a spelling hint for Whisper."""
    counts: Counter[str] = Counter()
    for t in texts:
        for w in re.findall(r"[A-Za-z][A-Za-z\-]{5,}", t):
            if w.lower() not in _STOP:
                counts[w] += 1
    common = {"increased", "patients", "disease", "adults", "people", "years", "including", "associated"}
    words = [w for w, _ in counts.most_common(400) if w.lower() not in common]
    seen, out = set(), []
    for w in words:
        key = w.lower()
        if key not in seen:
            seen.add(key)
            out.append(w)
        if len(out) >= limit:
            break
    return ", ".join(out)


def transcribe_reader1(wav: Path, glossary: str = "", model: str = "mlx-community/whisper-large-v3-turbo") -> list[dict[str, Any]]:
    import mlx_whisper  # local, Apple Silicon

    prompt = f"Glossary: {glossary}." if glossary else None
    result = mlx_whisper.transcribe(str(wav), path_or_hf_repo=model, word_timestamps=True, language="en",
                                    initial_prompt=prompt, condition_on_previous_text=True,
                                    temperature=0.0, verbose=None)
    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            text = w["word"].strip()
            if text:
                words.append({"text": text, "t0": float(w["start"]), "t1": float(w["end"]),
                              "conf": float(w.get("probability", 0.0))})
    return words


# ---------------------------------------------------------------- reader 2 (Gemini, audio only)

READER2_PROMPT = """Transcribe this audio verbatim, exactly as spoken, in English.
Rules:
- Write exactly what the speaker says. Do not correct, summarise or tidy the wording.
- Keep numbers as the speaker says them, using digits (for example "50 to 64", "94%").
- Use normal sentence punctuation.
- Return only the transcript text, no timestamps, no speaker labels, no commentary."""


def transcribe_reader2(gw: Gateway, audio: Path) -> str:
    from google.genai import types

    f = gem.upload(gw, audio)
    part = types.Part(file_data=types.FileData(file_uri=f.uri, mime_type=f.mime_type))
    call = gem.generate(gw, "asr.reader2", [part, READER2_PROMPT], purpose="asr.reader2")
    return call.text.strip()


# ---------------------------------------------------------------- consensus

_ATTACH = re.compile(r"^(-|%|'|’|\)|,|\.|;|:)")


def merge_fragments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whisper splits 'long-term' into 'long' + '-term' and '20%' into '20' + '%'. Join them."""
    out: list[dict[str, Any]] = []
    for w in words:
        if out and _ATTACH.match(w["text"]):
            prev = out[-1]
            prev["text"] = prev["text"] + w["text"]
            prev["t1"] = max(prev["t1"], w["t1"])
            prev["conf"] = min(prev.get("conf", 1.0), w.get("conf", 1.0))
        else:
            out.append(dict(w))
    return out


def word_key(text: str) -> str:
    """Comparison key for one displayed word: case, punctuation and hyphens do not count."""
    t = unicodedata.normalize("NFKC", text).lower()
    t = t.replace("percent", "").replace("per cent", "")
    stripped = re.sub(r"[^0-9a-z]+", "", t)
    if stripped in _ONES:
        return str(_ONES[stripped])
    if stripped in _TENS:
        return str(_TENS[stripped])
    return stripped


def consensus(words1: list[dict[str, Any]], text2: str, screen_text: str = "") -> list[dict[str, Any]]:
    """Align reader 2's words onto reader 1's timed words and decide each disagreement.

    Every output word keeps a time span and records: agree (1/0), alt (what the other reader
    heard) and chosen_from. Decisions:
      - reader 1 words that reader 2 did not hear are dropped when they have no duration or low
        confidence (Whisper inserts prompt words this way); otherwise kept and marked;
      - a replaced span takes reader 2's words when on-screen text supports them, or when
        reader 1 was unsure; otherwise reader 1 stays and the span is marked;
      - reader 2 words that reader 1 missed fill a pause if there is one, else stay as an alt.
    """
    w1 = [w for w in merge_fragments(words1) if word_key(w["text"])]
    d2 = [t for t in text2.split() if word_key(t)]
    k1 = [word_key(w["text"]) for w in w1]
    k2 = [word_key(t) for t in d2]
    screen_norm = " " + " ".join(normalize_tokens(screen_text)) + " "
    out: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=k1, b=k2, autojunk=False)
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        mine = [dict(w) for w in w1[a0:a1]]
        theirs = d2[b0:b1]
        if op == "equal":
            # timing from reader 1, wording and punctuation from reader 2 (they agree on the words)
            out += [dict(w, text=t, agree=1, alt=None, chosen_from="both") for w, t in zip(mine, theirs)]
            continue
        if mine and theirs and "".join(k1[a0:a1]) == "".join(k2[b0:b1]):
            # the same words split differently ("up to date" / "up-to-date"): agreement
            out += _spread(theirs, mine[0]["t0"], mine[-1]["t1"], chosen_from="both", agree=1)
            continue
        conf = sum(w.get("conf", 0.0) for w in mine) / len(mine) if mine else 1.0
        zero = any(w["t1"] - w["t0"] < 0.02 for w in mine)
        if op == "delete":
            if zero or conf < 0.6:
                continue  # reader 1 invented it; reader 2 heard nothing there
            out += [dict(w, agree=0, alt="", chosen_from="reader1") for w in mine]
            continue
        if op == "insert":
            prev_end = out[-1]["t1"] if out else 0.0
            next_start = w1[a0]["t0"] if a0 < len(w1) else prev_end + 0.5
            if next_start - prev_end >= 0.25:
                out += _spread(theirs, prev_end + 0.02, next_start - 0.02, chosen_from="reader2", agree=0)
            elif out:
                out[-1]["agree"] = 0
                out[-1]["alt"] = ((out[-1].get("alt") or out[-1]["text"]) + " +" + " ".join(theirs)).strip()
            continue
        # replace
        their_norm = " " + " ".join(normalize_tokens(" ".join(theirs))) + " "
        my_norm = " " + " ".join(normalize_tokens(" ".join(w["text"] for w in mine))) + " "
        supported = their_norm in screen_norm and my_norm not in screen_norm
        if supported or zero or conf < 0.6:
            out += _spread(theirs, mine[0]["t0"], mine[-1]["t1"], chosen_from="reader2", agree=0,
                           alt=" ".join(w["text"] for w in mine))
        else:
            out += [dict(w, agree=0, alt=" ".join(theirs) if i == 0 else "", chosen_from="reader1")
                    for i, w in enumerate(mine)]
    _repair_durations(out)
    log.info("asr consensus: %d words, %d disagree", len(out), sum(1 for w in out if not w["agree"]))
    return out


def _repair_durations(words: list[dict[str, Any]], min_dur: float = 0.08) -> None:
    """Whisper gives some words zero duration. Borrow time from the gap before, never overlap."""
    for i, w in enumerate(words):
        if w["t1"] - w["t0"] >= min_dur:
            continue
        prev_end = words[i - 1]["t1"] if i > 0 else max(0.0, w["t0"] - min_dur)
        next_start = words[i + 1]["t0"] if i + 1 < len(words) else w["t1"] + min_dur
        w["t0"] = max(prev_end, min(w["t0"], w["t1"] - min_dur))
        w["t1"] = min(next_start, max(w["t1"], w["t0"] + min_dur))
        if w["t1"] - w["t0"] < min_dur and i + 1 < len(words):
            nxt = words[i + 1]
            if nxt["t1"] - nxt["t0"] > 2 * min_dur:   # borrow the start of a long neighbour
                w["t1"] = w["t0"] + min_dur
                nxt["t0"] = w["t1"]


def _spread(texts: list[str], t0: float, t1: float, *, chosen_from: str, agree: int, alt: str | None = None) -> list[dict[str, Any]]:
    """Give each word a share of [t0, t1] by character length."""
    total = sum(len(t) for t in texts) or 1
    cursor, out = t0, []
    for i, t in enumerate(texts):
        dur = (t1 - t0) * len(t) / total
        out.append({"text": t, "t0": cursor, "t1": cursor + dur, "conf": 0.0, "agree": agree,
                    "alt": alt if i == 0 else "", "chosen_from": chosen_from})
        cursor += dur
    return out


def snap_to_pauses(words: list[dict[str, Any]], rms_t, rms_db, window: float = 0.35) -> int:
    """Whisper often reports touching words where the audio has a pause (sentence ends, commas).
    Where two words touch, move the boundary to the edges of a clearly quiet run nearby.
    Returns how many boundaries moved. The audio is the evidence; the model timing is a guess."""
    import numpy as np

    if len(rms_t) == 0:
        return 0
    moved = 0
    for a, b in zip(words, words[1:]):
        if b["t0"] - a["t1"] >= 0.1:
            continue
        lo, hi = a["t1"] - window, b["t0"] + window
        sel = (rms_t >= lo) & (rms_t <= hi)
        if not np.any(sel):
            continue
        ts, db = rms_t[sel], rms_db[sel]
        local = rms_db[(rms_t > lo - 1.5) & (rms_t < hi + 1.5)]
        speech = float(np.percentile(local, 70)) if len(local) else -20.0
        quiet = db <= min(-40.0, speech - 20.0)
        runs, run = [], []
        for t, q in zip(ts, quiet):
            if q:
                run.append(float(t))
            elif run:
                runs.append(run)
                run = []
        if run:
            runs.append(run)
        runs = [r for r in runs if r[-1] - r[0] >= 0.04]
        if not runs:
            continue
        mid = (a["t1"] + b["t0"]) / 2
        best = min(runs, key=lambda r: abs((r[0] + r[-1]) / 2 - mid))
        new_a1, new_b0 = best[0], best[-1]
        if new_a1 - a["t0"] >= 0.06 and b["t1"] - new_b0 >= 0.06:
            a["t1"], b["t0"] = new_a1, new_b0
            moved += 1
    return moved


def sentences(words: list[dict[str, Any]], max_s: float = 14.0) -> list[dict[str, Any]]:
    """Group words into sentences on terminal punctuation; split very long ones at commas."""
    out: list[dict[str, Any]] = []
    current: list[int] = []

    def close() -> None:
        if current:
            ws = [words[i] for i in current]
            agree = sum(w["agree"] for w in ws) / len(ws)
            out.append({"w0": current[0], "w1": current[-1] + 1, "t0": ws[0]["t0"], "t1": ws[-1]["t1"],
                        "text": " ".join(w["text"] for w in ws), "agree": agree})
            current.clear()

    for i, w in enumerate(words):
        current.append(i)
        text = w["text"]
        terminal = bool(re.search(r"[.!?][\"')\]]*$", text)) and not re.search(r"\b(?:e\.g|i\.e|vs|Dr|et al)\.$", text)
        too_long = (w["t1"] - words[current[0]]["t0"]) > max_s and text.endswith((",", ";", ":"))
        if terminal or too_long:
            close()
    close()
    return out
