import os
import unittest

from qqcache import login

HERE = os.path.join(os.path.dirname(__file__), "fixtures_png")


def _png(name):
    with open(os.path.join(HERE, name), "rb") as fh:
        return fh.read()


class QrPixelTest(unittest.TestCase):
    """真机截图：3.2.33 登录页的有效码 / 过期遮罩（裸 a11y 树时的唯一判据）。"""

    def test_decoder(self):
        w, h, rows = login.png_rgb(_png("qr_expired.png"))
        self.assertEqual((w, h), (156, 156))
        self.assertEqual(len(rows[0]), 156 * 3)

    def test_expired_overlay_detected(self):
        self.assertTrue(login.qr_pixels_expired(_png("qr_expired.png")))

    def test_valid_code_not_expired(self):
        self.assertFalse(login.qr_pixels_expired(_png("qr_valid.png")))

    def test_blank_or_garbage_not_expired(self):
        self.assertFalse(login.qr_pixels_expired(b"not a png"))
        self.assertFalse(login.qr_pixels_expired(None))
