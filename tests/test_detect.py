import cv2
import numpy as np

from attic.ui.webcam.detect import (
    Circle,
    crop_circle,
    detect_circle,
    detect_rectangle,
    four_point_transform,
    order_quad,
)


def test_order_quad():
    pts = np.array([[10, 10], [100, 12], [98, 90], [8, 88]])
    ordered = order_quad(pts)
    # top-left, top-right, bottom-right, bottom-left
    assert tuple(ordered[0]) == (10, 10)
    assert tuple(ordered[2]) == (98, 90)


def test_detect_rectangle_on_synthetic():
    img = np.zeros((300, 400, 3), dtype=np.uint8)
    cv2.rectangle(img, (60, 50), (340, 250), (255, 255, 255), -1)
    quad = detect_rectangle(img)
    assert quad is not None
    # Corners should be near the drawn rectangle's extent.
    xs = sorted(quad[:, 0])
    ys = sorted(quad[:, 1])
    assert xs[0] < 80 and xs[-1] > 320
    assert ys[0] < 70 and ys[-1] > 230


def test_detect_rectangle_none_on_blank():
    img = np.zeros((200, 200, 3), dtype=np.uint8)
    assert detect_rectangle(img) is None


def test_four_point_transform_output_size():
    img = np.zeros((300, 400, 3), dtype=np.uint8)
    cv2.rectangle(img, (60, 50), (340, 250), (255, 255, 255), -1)
    quad = np.array([[60, 50], [340, 50], [340, 250], [60, 250]], dtype="float32")
    warped = four_point_transform(img, quad)
    # ~280 wide x ~200 tall.
    assert abs(warped.shape[1] - 280) <= 2
    assert abs(warped.shape[0] - 200) <= 2


def test_detect_circle_on_synthetic():
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    cv2.circle(img, (200, 200), 120, (255, 255, 255), -1)
    circle = detect_circle(img)
    assert circle is not None
    assert abs(circle.x - 200) < 20
    assert abs(circle.y - 200) < 20
    assert abs(circle.r - 120) < 30


def test_detect_circle_none_on_blank():
    assert detect_circle(np.zeros((200, 200, 3), dtype=np.uint8)) is None


def test_crop_circle_bounds():
    img = np.zeros((400, 400, 3), dtype=np.uint8)
    crop = crop_circle(img, Circle(200, 200, 100))
    # 2*r + 2*pad ~ 208 per side, clamped within image.
    assert crop.shape[0] <= 400 and crop.shape[1] <= 400
    assert crop.shape[0] > 150 and crop.shape[1] > 150


def test_crop_circle_clamps_at_edges():
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    crop = crop_circle(img, Circle(10, 10, 80))
    assert crop.shape[0] <= 100 and crop.shape[1] <= 100
    assert crop.size > 0
