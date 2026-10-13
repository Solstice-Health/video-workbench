"""The asset lab: rebuild assets from the source at the lowest-provenance rung that passes.

Built here: rung 0 (best decoded instance, original pixels) and rung 1 (temporal median
of registered instances, fused pixels). Rungs 2-5 (SAM 3.1 mattes, image-model split,
tracing, keyed motion) are planned; rung 6 asks for the source file (NEEDS-SOURCE).
"""
