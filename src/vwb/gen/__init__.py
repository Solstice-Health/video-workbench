"""Generative toolkit: the image model ("sunburst") and the video model (Omni), with the measured
guards around them.

  refs     resolve image references (source frames, crops, registry assets) to RGBA arrays
  images   sunburst: create elements with alpha, extract an element, masked inpaint, outpaint
  video    Omni: text, first/last frame, reference, edit and extend clips
  key      green-screen (or other key colour) plates in, alpha clips out
  qa       measurements every generated asset carries: alpha, text found, faces, seams, fidelity

A model only proposes pixels. Code keeps source pixels outside every mask, measures what the
model changed, and stores the result in the registry with its lineage; the verifier raises the
provenance flags when an output uses it.
"""
