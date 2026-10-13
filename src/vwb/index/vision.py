"""Apple Vision on Apple Silicon: text, faces, people and attention saliency in one pass.

Vision returns normalised boxes with a bottom-left origin; everything leaving this module
is in top-left pixel coordinates of the analysed image. Language correction is off for
OCR, because a corrector reads a misspelling as the intended word (spec R17).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import numpy as np

try:  # macOS only
    import objc
    import Quartz
    import Vision
    HAVE_VISION = True
except ImportError:  # pragma: no cover - non-macOS hosts
    HAVE_VISION = False

_lock = threading.Lock()


@dataclass
class TextLine:
    text: str
    conf: float
    box: tuple[float, float, float, float]  # x, y, w, h in pixels


@dataclass
class VisionResult:
    lines: list[TextLine] = field(default_factory=list)
    faces: list[tuple[tuple[float, float, float, float], float]] = field(default_factory=list)
    humans: list[tuple[tuple[float, float, float, float], float]] = field(default_factory=list)
    salient: list[tuple[tuple[float, float, float, float], float]] = field(default_factory=list)


def _cgimage(rgb: np.ndarray):
    h, w = rgb.shape[:2]
    rgba = np.ascontiguousarray(np.dstack([rgb, np.full((h, w), 255, np.uint8)]))
    data = rgba.tobytes()
    provider = Quartz.CGDataProviderCreateWithData(None, data, len(data), None)
    space = Quartz.CGColorSpaceCreateDeviceRGB()
    img = Quartz.CGImageCreate(w, h, 8, 32, w * 4, space, Quartz.kCGImageAlphaNoneSkipLast,
                               provider, None, False, Quartz.kCGRenderingIntentDefault)
    return img, data  # keep `data` alive while Vision reads the image


def _px(bb, w: int, h: int) -> tuple[float, float, float, float]:
    x, y = bb.origin.x * w, (1.0 - bb.origin.y - bb.size.height) * h
    return (float(x), float(y), float(bb.size.width * w), float(bb.size.height * h))


def analyze(rgb: np.ndarray, *, ocr: bool = True, faces: bool = True, humans: bool = True,
            saliency: bool = True, fast_ocr: bool = False) -> VisionResult:
    if not HAVE_VISION:
        raise RuntimeError("Apple Vision is not available on this host")
    h, w = rgb.shape[:2]
    out = VisionResult()
    with objc.autorelease_pool():
        img, _keep = _cgimage(rgb)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
        requests = []
        r_ocr = r_face = r_hum = r_sal = None
        if ocr:
            r_ocr = Vision.VNRecognizeTextRequest.alloc().init()
            r_ocr.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast if fast_ocr
                                       else Vision.VNRequestTextRecognitionLevelAccurate)
            r_ocr.setUsesLanguageCorrection_(False)
            r_ocr.setRecognitionLanguages_(["en-US"])
            r_ocr.setMinimumTextHeight_(0.0)
            requests.append(r_ocr)
        if faces:
            r_face = Vision.VNDetectFaceRectanglesRequest.alloc().init()
            requests.append(r_face)
        if humans:
            r_hum = Vision.VNDetectHumanRectanglesRequest.alloc().init()
            r_hum.setUpperBodyOnly_(False)
            requests.append(r_hum)
        if saliency:
            r_sal = Vision.VNGenerateAttentionBasedSaliencyImageRequest.alloc().init()
            requests.append(r_sal)
        ok, err = handler.performRequests_error_(requests, None)
        if not ok:
            raise RuntimeError(f"Vision request failed: {err}")
        if r_ocr is not None:
            for obs in r_ocr.results() or []:
                cand = obs.topCandidates_(1)
                if cand:
                    c = cand[0]
                    out.lines.append(TextLine(str(c.string()), float(c.confidence()), _px(obs.boundingBox(), w, h)))
        if r_face is not None:
            out.faces = [(_px(o.boundingBox(), w, h), float(o.confidence())) for o in (r_face.results() or [])]
        if r_hum is not None:
            out.humans = [(_px(o.boundingBox(), w, h), float(o.confidence())) for o in (r_hum.results() or [])]
        if r_sal is not None:
            for res in r_sal.results() or []:
                for o in res.salientObjects() or []:
                    out.salient.append((_px(o.boundingBox(), w, h), float(o.confidence())))
    return out
