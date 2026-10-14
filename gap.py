"""Slider gap detection (OpenCV edge template matching, replacing ddddocr/onnxruntime).

ByteDance slider: the background jpeg has a groove shadow, and the slider piece png has an alpha channel.
Use the slider piece's alpha contour as a template and match it against the background Canny edge map,
returning the x of the gap's left edge (in the background's natural coordinates).
"""
import cv2
import numpy as np


def find_gap_x(bg_bytes: bytes, piece_bytes: bytes) -> tuple:
    """Return (gap_x, confidence). gap_x is the left edge of the gap in the background image's natural pixel coordinates."""
    bg = cv2.imdecode(np.frombuffer(bg_bytes, np.uint8), cv2.IMREAD_COLOR)
    piece = cv2.imdecode(np.frombuffer(piece_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if piece is None or bg is None:
        raise ValueError("Image decoding failed")

    if piece.shape[2] == 4:
        alpha = piece[:, :, 3]
    else:
        alpha = cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY)

    piece_edge = cv2.Canny(alpha, 100, 200)
    bg_edge = cv2.Canny(cv2.cvtColor(bg, cv2.COLOR_BGR2GRAY), 100, 200)

    # The template must not be larger than the background
    if piece_edge.shape[0] > bg_edge.shape[0] or piece_edge.shape[1] > bg_edge.shape[1]:
        raise ValueError("Slider piece is larger than the background; abnormal dimensions")

    res = cv2.matchTemplate(bg_edge, piece_edge, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    return int(max_loc[0]), float(max_val)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    bg = Path(sys.argv[1] if len(sys.argv) > 1 else "dbg_bg.jpg").read_bytes()
    piece = Path(sys.argv[2] if len(sys.argv) > 2 else "dbg_piece.png").read_bytes()
    x, conf = find_gap_x(bg, piece)
    print(f"gap_x={x} conf={conf:.3f}")