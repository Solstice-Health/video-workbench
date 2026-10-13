"""The inspection engine (spec D42). Both the editor's inner critic and, later, the sealed
judge call these checks. Checks return findings with evidence; they never accept anything.

Tiers (spec D40): T0 lint on every render (delivery and degenerate-edit gates), T1 declared
checks (what the canvas makes the frame show) and observed checks (what the delivered file
shows: OCR read-back of typeset text, re-render attestation, ASR read-back of the audio).
"""

from vwb.qc.findings import Finding

__all__ = ["Finding"]
