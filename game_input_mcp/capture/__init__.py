from . import dxcam_backend, mss_backend, pillow_backend, wgc
from .base import CaptureBackend, CaptureError, CaptureResult, capture_region

__all__ = ["CaptureBackend", "CaptureError", "CaptureResult", "capture_region"]
