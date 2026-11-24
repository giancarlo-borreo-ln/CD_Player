"""One-off: render the dvd-player desktop icon (Xbox-green DVD glyph).

Usage: uv run python packaging/make_icon.py [output.png]
"""
import sys
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QColor, QImage, QPainter, QPen

out = Path(sys.argv[1] if len(sys.argv) > 1 else
           Path.home() / ".local/share/icons/hicolor/256x256/apps/dvd-player.png")
out.parent.mkdir(parents=True, exist_ok=True)

img = QImage(256, 256, QImage.Format.Format_ARGB32)
img.fill(QColor(16, 16, 16))
p = QPainter(img)
p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

# rounded dark tile
p.setBrush(QColor(24, 24, 24))
p.setPen(Qt.PenStyle.NoPen)
p.drawRoundedRect(QRectF(8, 8, 240, 240), 48, 48)

# Xbox-green playback ring
p.setBrush(Qt.BrushStyle.NoBrush)
pen = QPen(QColor(116, 195, 41), 14)
pen.setCapStyle(Qt.PenCapStyle.RoundCap)
p.setPen(pen)
p.drawArc(QRectF(48, 48, 160, 160), 40 * 16, 280 * 16)

# disc
p.setBrush(QColor(210, 214, 220))
p.setPen(Qt.PenStyle.NoPen)
p.drawEllipse(QRectF(72, 72, 112, 112))
p.setBrush(QColor(16, 16, 16))
p.drawEllipse(QRectF(120, 120, 16, 16))

# green play glyph on the disc
p.setBrush(QColor(116, 195, 41))
poly = [__import__("PyQt6.QtCore", fromlist=["QPointF"]).QPointF(x, y)
        for x, y in ((108, 92), (108, 164), (160, 128))]
p.drawPolygon(poly)
p.end()

img.save(str(out))
print("icon written:", out)
