"""SPOT vision/OCR service: RapidOCR + code readers (thermal, gauge, spectrogram, Orbit Key Results).

Contract: docs/vision-contract.md. Code produces every number; this package never calls a model.
"""
SERVICE_VERSION = "0.2.0"   # 0.2.0: gauge UNIT_UNKNOWN (no silent psi), SHORT_NEEDLE
CONTRACT_VERSION = "2026-10-10.vision.2"
