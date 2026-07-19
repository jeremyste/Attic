"""Classical-CV detection for cropping media photos (no ML model).

Two shapes:
  * Rectangular media (floppy, HDD): find the largest 4-sided contour and apply a
    perspective transform to straighten/crop it.
  * Circular media (CD/DVD): Hough circle detection, crop to the disc.

All functions are pure (numpy arrays in/out) so they can be unit-tested on
synthetic images without a camera. The Qt widget in ``capture_widget`` drives
these against live frames.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Circle:
    x: int
    y: int
    r: int


def order_quad(pts: np.ndarray) -> np.ndarray:
    """Order 4 points as top-left, top-right, bottom-right, bottom-left."""
    pts = pts.reshape(4, 2).astype("float32")
    ordered = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    ordered[0] = pts[np.argmin(s)]  # top-left has smallest x+y
    ordered[2] = pts[np.argmax(s)]  # bottom-right has largest x+y
    diff = np.diff(pts, axis=1).ravel()
    ordered[1] = pts[np.argmin(diff)]  # top-right has smallest y-x
    ordered[3] = pts[np.argmax(diff)]  # bottom-left has largest y-x
    return ordered


def detect_rectangle(image: np.ndarray, *, min_area_frac: float = 0.05) -> np.ndarray | None:
    """Return an ordered 4-point quad of the largest rectangular object, or None.

    ``min_area_frac`` rejects tiny contours (fraction of the image area).
    """
    if image is None or image.size == 0:
        return None
    gray = _to_gray(image)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 50, 150)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    img_area = image.shape[0] * image.shape[1]
    best = None
    best_area = min_area_frac * img_area
    for c in contours:
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            area = cv2.contourArea(approx)
            if area > best_area:
                best_area = area
                best = approx
    if best is None:
        return None
    return order_quad(best)


def four_point_transform(image: np.ndarray, quad: np.ndarray) -> np.ndarray:
    """Perspective-transform ``image`` to a top-down view of ``quad``."""
    rect = order_quad(quad)
    (tl, tr, br, bl) = rect
    width = int(max(np.linalg.norm(br - bl), np.linalg.norm(tr - tl)))
    height = int(max(np.linalg.norm(tr - br), np.linalg.norm(tl - bl)))
    width = max(width, 1)
    height = max(height, 1)
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(rect, dst)
    return cv2.warpPerspective(image, matrix, (width, height))


def detect_circle(image: np.ndarray, *, min_radius_frac: float = 0.15) -> Circle | None:
    """Return the most prominent circle (disc), or None.

    ``min_radius_frac`` sets the minimum radius as a fraction of the smaller
    image dimension.
    """
    if image is None or image.size == 0:
        return None
    gray = _to_gray(image)
    gray = cv2.medianBlur(gray, 5)
    min_dim = min(image.shape[:2])
    min_r = int(min_radius_frac * min_dim)
    circles = cv2.HoughCircles(
        gray, cv2.HOUGH_GRADIENT, dp=1.2, minDist=min_dim,
        param1=100, param2=30, minRadius=min_r, maxRadius=0,
    )
    if circles is None:
        return None
    # HoughCircles returns strongest first.
    x, y, r = np.round(circles[0][0]).astype(int)
    return Circle(x=int(x), y=int(y), r=int(r))


def crop_circle(image: np.ndarray, circle: Circle, *, pad: int = 4) -> np.ndarray:
    """Crop a square bounding box around ``circle`` (clamped to image bounds)."""
    h, w = image.shape[:2]
    x0 = max(circle.x - circle.r - pad, 0)
    y0 = max(circle.y - circle.r - pad, 0)
    x1 = min(circle.x + circle.r + pad, w)
    y1 = min(circle.y + circle.r + pad, h)
    return image[y0:y1, x0:x1]


def _to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
