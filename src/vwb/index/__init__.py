"""The edit index: an open evidence store about each source video (spec D19, D35).

Deterministic facts come first (shots, words, on-screen text, faces, saliency, layout
blocks, audio). Model readings are stored as claims. Agents add observations as they
explore. Every row carries its producer and a status: fact, reproduced or claim.
"""
