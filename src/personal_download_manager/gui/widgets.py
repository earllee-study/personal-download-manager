"""Small custom widgets/painters used by the main window."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate

from .model import (
    STATUS_COMPLETED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PAUSED,
    STATUS_RETRYING,
    STATUS_SCHEDULED,
    ProgressRole,
    TaskRole,
)


def make_app_icon(size: int = 64) -> QIcon:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor("#3D9BFF"))
    gradient.setColorAt(1.0, QColor("#0060DF"))
    painter.setBrush(gradient)
    painter.drawRoundedRect(3, 3, size - 6, size - 6, size * 0.24, size * 0.24)
    pen = QPen(QColor("#ffffff"), max(3.0, size * 0.08))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    x = size / 2
    painter.drawLine(x, size * 0.24, x, size * 0.66)
    painter.drawLine(size * 0.32, size * 0.50, x, size * 0.70)
    painter.drawLine(size * 0.68, size * 0.50, x, size * 0.70)
    painter.end()
    return QIcon(pixmap)


def symbol_icon(name: str, color: QColor | None = None) -> QIcon:
    """A small monochrome line icon in the spirit of SF Symbols.

    Drawn at 2x for crisp scaling; the color follows the current palette, so
    light/dark themes get readable toolbar icons.
    """
    if color is None:
        app = QApplication.instance()
        color = (
            app.palette().color(QPalette.ColorRole.WindowText)
            if app is not None
            else QColor("#1D1D1F")
        )
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(2.0, 2.0)
    pen = QPen(color, 2.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    def line(x1, y1, x2, y2):
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def poly(points, fill=False):
        painter.setBrush(color if fill else Qt.BrushStyle.NoBrush)
        painter.setPen(Qt.PenStyle.NoPen if fill else pen)
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))
        if not fill:
            painter.setPen(pen)

    if name == "add":
        line(16, 5, 16, 20)
        line(10, 14, 16, 20)
        line(22, 14, 16, 20)
        line(8, 24, 8, 27)
        line(8, 27, 24, 27)
        line(24, 24, 24, 27)
    elif name == "play":
        poly([(11, 6), (25, 16), (11, 26)], fill=True)
    elif name == "pause":
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(10, 6, 4, 20), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(18, 6, 4, 20), 1.5, 1.5)
    elif name == "trash":
        line(9, 10, 23, 10)
        line(13, 7, 19, 7)
        path = QPainterPath()
        path.moveTo(11, 10)
        path.lineTo(12, 26)
        path.quadTo(12, 27, 13, 27)
        path.lineTo(19, 27)
        path.quadTo(20, 27, 20, 26)
        path.lineTo(21, 10)
        painter.drawPath(path)
        line(14, 14, 14, 23)
        line(18, 14, 18, 23)
    elif name == "file":
        path = QPainterPath()
        path.moveTo(10, 5)
        path.lineTo(20, 5)
        path.lineTo(25, 10)
        path.lineTo(25, 27)
        path.lineTo(10, 27)
        path.closeSubpath()
        painter.drawPath(path)
        line(20, 5, 20, 10)
        line(20, 10, 25, 10)
        line(14, 15, 21, 15)
        line(14, 19, 21, 19)
    elif name == "folder":
        path = QPainterPath()
        path.moveTo(5, 11)
        path.quadTo(5, 9, 7, 9)
        path.lineTo(13, 9)
        path.lineTo(15, 12)
        path.lineTo(25, 12)
        path.quadTo(27, 12, 27, 14)
        path.lineTo(27, 24)
        path.quadTo(27, 26, 25, 26)
        path.lineTo(7, 26)
        path.quadTo(5, 26, 5, 24)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "clock":
        painter.drawEllipse(QRectF(6, 6, 20, 20))
        line(16, 11, 16, 17)
        line(16, 17, 21, 19)
    elif name == "sliders":
        for y, knob in ((10, 12), (16, 20), (22, 15)):
            line(6, y, 26, y)
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(QPointF(knob, y), 2.6, 2.6)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "check":
        poly([(7, 17), (13, 23), (25, 9)])
    elif name == "copy":
        painter.drawRoundedRect(QRectF(10, 12, 13, 15), 2.5, 2.5)
        painter.drawRoundedRect(QRectF(15, 5, 12, 15), 2.5, 2.5)
    elif name == "info":
        painter.drawEllipse(QRectF(6, 6, 20, 20))
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(16, 11), 1.6, 1.6)
        painter.setPen(pen)
        line(16, 15, 16, 22)
    elif name == "refresh":
        painter.drawArc(QRectF(7, 7, 18, 18), 50 * 16, 280 * 16)
        poly([(22, 5), (26, 10), (20, 11)], fill=True)
    elif name == "batch":
        painter.drawRoundedRect(QRectF(6, 7, 14, 16), 2.5, 2.5)
        painter.drawRoundedRect(QRectF(12, 11, 14, 16), 2.5, 2.5)
        line(19, 14, 19, 22)
        line(15, 18, 23, 18)
    else:
        painter.drawEllipse(QRectF(8, 8, 16, 16))
    painter.end()
    return QIcon(pixmap)


def standard_icon(widget, *names) -> QIcon:
    for name in names:
        value = getattr(QStyle.StandardPixmap, name, None)
        if value is not None:
            return widget.style().standardIcon(value)
    return QIcon()


class ProgressDelegate(QStyledItemDelegate):
    """Draw a rounded progress bar with the percentage inside."""

    def paint(self, painter: QPainter, option, index) -> None:
        progress = index.data(ProgressRole)
        if progress is None:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        app = QApplication.instance()
        if app is not None:
            dark = (
                app.palette().color(QPalette.ColorRole.Window).lightness() < 128
            )
        else:
            dark = option.palette.color(QPalette.ColorRole.Window).lightness() < 128
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            painter.fillRect(
                option.rect, option.palette.color(QPalette.ColorRole.Highlight)
            )

        # A thin, rounded macOS-style track; the percentage sits to the right.
        text_width = 46
        height = 7.0
        track = QRectF(
            option.rect.left() + 10,
            option.rect.center().y() - height / 2,
            max(20, option.rect.width() - 10 - text_width - 8),
            height,
        )
        radius = height / 2
        painter.setPen(Qt.PenStyle.NoPen)
        if selected:
            painter.setBrush(QColor(255, 255, 255, 70))
        else:
            painter.setBrush(QColor("#3A3A3C") if dark else QColor("#E5E5EA"))
        painter.drawRoundedRect(track, radius, radius)

        task = index.data(TaskRole)
        status = task.status if task is not None else None
        if status == STATUS_COMPLETED:
            color = "#30D158" if dark else "#34C759"
        elif status == STATUS_FAILED:
            color = "#FF453A" if dark else "#FF3B30"
        elif status == STATUS_PAUSED:
            color = "#FF9F0A"
        elif status == STATUS_DOWNLOADING:
            color = "#0A84FF" if dark else "#007AFF"
        elif status == STATUS_SCHEDULED:
            color = "#BF5AF2" if dark else "#AF52DE"
        elif status == STATUS_RETRYING:
            color = "#FF9F0A"
        else:
            color = "#98989D" if dark else "#8E8E93"

        fraction = max(0.0, min(100.0, float(progress))) / 100.0
        fill = QRectF(track)
        fill.setWidth(track.width() * fraction)
        if fraction > 0:
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(fill, radius, radius)

        text = f"{float(progress):.1f}%"
        text_rect = option.rect.adjusted(
            option.rect.width() - text_width, 0, 0, 0
        )
        painter.setPen(
            option.palette.color(
                QPalette.ColorRole.HighlightedText
                if selected
                else QPalette.ColorRole.Text
            )
        )
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.restore()
