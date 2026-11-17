"""
camera.py
─────────
Thin wrapper around OpenCV's VideoCapture so the rest of the system
doesn't have to care whether the source is a webcam index or an RTSP URL.

Usage
-----
    from camera import CameraSource

    cam = CameraSource(device=0)          # first webcam
    cam.open()

    ret, frame = cam.read()
    fps        = cam.fps                  # reported by the driver (fallback: 30)
    cam.release()
"""

import cv2


class CameraSource:
    """
    Wraps cv2.VideoCapture with automatic reconnect helpers.

    Parameters
    ----------
    device : int | str
        Camera index (0, 1, …) or an RTSP/HTTP URL.
    width, height : int | None
        Requested capture resolution.  The driver may ignore these.
    fps_hint : int | None
        Requested frame-rate; used only as a fallback if the driver
        reports 0 fps.
    backend : int
        OpenCV capture backend.  Use cv2.CAP_DSHOW on Windows for
        lower latency; cv2.CAP_ANY elsewhere.
    """

    def __init__(
        self,
        device: int | str = 0,
        width: int | None = 1280,
        height: int | None = 720,
        fps_hint: int = 30,
        backend: int = cv2.CAP_ANY,
    ):
        self.device    = device
        self.width     = width
        self.height    = height
        self.fps_hint  = fps_hint
        self.backend   = backend
        self._cap: cv2.VideoCapture | None = None

    # ── public API ────────────────────────────────────────────────────────

    def open(self) -> bool:
        """Open (or re-open) the capture device.  Returns True on success."""
        self.release()
        cap = cv2.VideoCapture(self.device, self.backend)
        if not cap.isOpened():
            print(f"[Camera] ERROR: Cannot open device {self.device!r}")
            return False

        if self.width and self.height:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

        self._cap = cap
        print(
            f"[Camera] Opened device {self.device!r} — "
            f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}×"
            f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} @ {self.fps:.1f} fps"
        )
        return True

    def read(self) -> tuple[bool, object]:
        """Read one frame.  Returns (True, frame) or (False, None)."""
        if self._cap is None or not self._cap.isOpened():
            return False, None
        return self._cap.read()

    def release(self):
        """Release the capture device."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def is_opened(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @property
    def fps(self) -> float:
        """Actual fps reported by the driver, or fps_hint as fallback."""
        if self._cap is None:
            return float(self.fps_hint)
        v = self._cap.get(cv2.CAP_PROP_FPS)
        return v if v and v > 0 else float(self.fps_hint)

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def list_devices(max_index: int = 8) -> list[int]:
        """
        Return a list of webcam indices that OpenCV can open.
        Useful for choosing which camera to use.
        """
        available = []
        for idx in range(max_index):
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if cap.isOpened() and cap.read()[0]:
                available.append(idx)
            cap.release()
        return available


if __name__ == "__main__":
    devices = CameraSource.list_devices()
    print("Available capture devices:", devices)

    if devices:
        cam = CameraSource(device=devices[0])
        if cam.open():
            import time
            for _ in range(5):
                ret, frame = cam.read()
                print("Frame OK:", ret, "—", frame.shape if ret else "N/A")
                time.sleep(0.1)
            cam.release()