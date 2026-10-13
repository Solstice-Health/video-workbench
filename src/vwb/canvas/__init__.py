"""The canvas file: one immutable, versioned description of an output (spec D37).

A fixed core (time map, layer stack, layer records with provenance, copy deck) and open
generator kinds. Generators never put code in the canvas: they render a hashed alpha plate
and a layer cites the hash. The verifier reads only the core, so it can explain every pixel.
"""

from vwb.canvas.model import Canvas, CopyEntry, Layer, Span

__all__ = ["Canvas", "CopyEntry", "Layer", "Span"]
