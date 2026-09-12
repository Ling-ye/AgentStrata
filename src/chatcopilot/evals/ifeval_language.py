"""Private deterministic detector; detection failures are scoring errors."""

from langdetect import LangDetectException
from langdetect.detector_factory import DetectorFactory, PROFILES_DIRECTORY


def detect(text: str) -> str:
    factory = DetectorFactory()
    factory.seed = 0
    factory.load_profile(PROFILES_DIRECTORY)
    detector = factory.create()
    try:
        detector.append(text)
        return detector.detect()
    except LangDetectException as exc:
        raise ValueError("IFEval language detection failed") from exc
