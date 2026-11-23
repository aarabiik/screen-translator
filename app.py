import sys
import time
import json
import asyncio
import urllib.request
import urllib.parse
from PIL import Image
import winocr
import keyboard

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QRect
from PyQt6.QtGui import (
    QPainter, QColor, QFont, QPen, QBrush, QCursor,
    QIcon, QPixmap, QAction
)
from PyQt6.QtWidgets import QApplication, QWidget, QMenu, QSystemTrayIcon

# Поддержка Per-Monitor DPI
try:
    import ctypes
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

TRANSLATION_CACHE = {}

def batch_translate(texts, sl="en", tl="ru"):
    to_fetch = []
    indices = []
    results = [None] * len(texts)

    for i, t in enumerate(texts):
        cleaned = t.strip()
        if not cleaned:
            results[i] = ""
            continue
        key = (cleaned, sl, tl)
        if key in TRANSLATION_CACHE:
            results[i] = TRANSLATION_CACHE[key]
        else:
            indices.append(i)
            to_fetch.append(cleaned)

    if not to_fetch:
        return results

    delimiter = "\n=====\n"
    bundle = delimiter.join(to_fetch)

    try:
        url = (
            "https://translate.googleapis.com/translate_a/single?client=gtx&sl="
            + sl + "&tl=" + tl + "&dt=t&q=" + urllib.parse.quote(bundle)
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            full_trans = "".join([part[0] for part in data[0] if part[0]])
            parts = full_trans.split(delimiter)

            for idx, orig_text in zip(indices, to_fetch):
                if parts:
                    tr = parts.pop(0).strip().replace("\n", " ")
                else:
                    tr = orig_text
                TRANSLATION_CACHE[(orig_text, sl, tl)] = tr
                results[idx] = tr
    except Exception:
        for idx, orig_text in zip(indices, to_fetch):
            results[idx] = orig_text

    return results


class GlobalHotkeyThread(QThread):
    trigger_snipping = pyqtSignal()
    clear_hud = pyqtSignal()

    def run(self):
        try:
            keyboard.add_hotkey("alt+q", lambda: self.trigger_snipping.emit())
            keyboard.add_hotkey("ctrl+shift+t", lambda: self.trigger_snipping.emit())
            keyboard.add_hotkey("esc", lambda: self.clear_hud.emit())
            # Sleep 0.2с в цикле ожидания потребляет 0.0% CPU
            while True:
                time.sleep(0.2)
        except Exception:
            pass


class SnippingOverlay(QWidget):
    region_selected = pyqtSignal(QRect)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))
        self.start_pos = None
        self.end_pos = None
        self.is_selecting = False

    def start_selection(self):
        screen_geo = QApplication.primaryScreen().geometry()
        self.setGeometry(screen_geo)
        self.start_pos = None
        self.end_pos = None
        self.is_selecting = False
        self.showFullScreen()
        self.activateWindow()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.start_pos = event.position().toPoint()
            self.end_pos = self.start_pos
            self.is_selecting = True
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self.hide()

    def mouseMoveEvent(self, event):
        if self.is_selecting:
            self.end_pos = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.is_selecting:
            self.is_selecting = False
            self.hide()
            rect = QRect(self.start_pos, self.end_pos).normalized()
            if rect.width() > 15 and rect.height() > 10:
                self.region_selected.emit(rect)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))

        painter.setPen(QColor(255, 255, 255, 230))
        painter.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        painter.drawText(
            self.rect(),
            int(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter),
            "\nВыделите область текста на экране (Esc / Правый клик — отмена)"
        )

        if self.start_pos and self.end_pos:
            rect = QRect(self.start_pos, self.end_pos).normalized()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.fillRect(rect, Qt.GlobalColor.transparent)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            pen = QPen(QColor(66, 133, 244), 2, Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            painter.drawRect(rect)


class InPlaceTranslationOverlay(QWidget):
    def __init__(self):
        super().__init__()
        self.blocks = []
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.WindowTransparentForInput |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        screen_geo = QApplication.primaryScreen().geometry()
        self.setGeometry(screen_geo)

    def set_blocks(self, blocks):
        self.blocks = blocks
        self.show()
        self.update()

    def clear_blocks(self):
        self.blocks = []
        self.update()
        self.hide()

    def paintEvent(self, event):
        if not self.blocks:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg_brush = QBrush(QColor(18, 18, 26, 245))
        border_pen = QPen(QColor(66, 133, 244, 220), 1)
        text_pen = QPen(QColor(255, 255, 255))

        for b in self.blocks:
            rx, ry, rw, rh = b["rect"]
            text = b["trans"]

            font_size = max(10, min(16, int(rh * 0.72)))
            font = QFont("Segoe UI", font_size, QFont.Weight.DemiBold)
            painter.setFont(font)

            metrics = painter.fontMetrics()
            text_w = metrics.horizontalAdvance(text)
            text_h = metrics.height()

            pad_x = 5
            pad_y = 3
            render_w = max(rw + pad_x * 2, text_w + pad_x * 2)
            render_h = max(rh + pad_y * 2, text_h + pad_y * 2)

            target_rect = QRect(rx - pad_x, ry - pad_y, render_w, render_h)

            painter.setBrush(bg_brush)
            painter.setPen(border_pen)
            painter.drawRoundedRect(target_rect, 4, 4)

            painter.setPen(text_pen)
            painter.drawText(
                target_rect,
                int(Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine),
                text
            )


class AppController:
    def __init__(self, app):
        self.app = app
        self.snipper = SnippingOverlay()
        self.hud = InPlaceTranslationOverlay()

        self.snipper.region_selected.connect(self.process_selected_region)

        # Трей-иконка (в правом нижнем углу Windows около часов)
        self.setup_tray()

        self.hotkey_thread = GlobalHotkeyThread()
        self.hotkey_thread.trigger_snipping.connect(self.snipper.start_selection)
        self.hotkey_thread.clear_hud.connect(self.hud.clear_blocks)
        self.hotkey_thread.start()

    def setup_tray(self):
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(66, 133, 244))
        painter = QPainter(pixmap)
        painter.setPen(QColor(255, 255, 255))
        painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "G")
        painter.end()

        self.tray = QSystemTrayIcon(QIcon(pixmap), self.app)
        self.tray.setToolTip("Google Screen Translator (Alt + Q)")

        menu = QMenu()
        translate_act = QAction("Перевести область (Alt + Q)", menu)
        translate_act.triggered.connect(self.snipper.start_selection)
        menu.addAction(translate_act)

        clear_act = QAction("Убрать перевод с экрана (Esc)", menu)
        clear_act.triggered.connect(self.hud.clear_blocks)
        menu.addAction(clear_act)

        menu.addSeparator()
        quit_act = QAction("Выход", menu)
        quit_act.triggered.connect(self.app.quit)
        menu.addAction(quit_act)

        self.tray.setContextMenu(menu)
        self.tray.show()

    def process_selected_region(self, rect: QRect):
        screen = QApplication.primaryScreen()
        pixmap = screen.grabWindow(0, rect.x(), rect.y(), rect.width(), rect.height())

        qimg = pixmap.toImage().convertToFormat(pixmap.toImage().Format.Format_RGBA8888)
        w, h = qimg.width(), qimg.height()
        ptr = qimg.bits()
        ptr.setsize(h * w * 4)
        img = Image.frombuffer("RGBA", (w, h), bytes(ptr), "raw", "RGBA", 0, 1)

        async def do_ocr():
            return await winocr.recognize_pil(img, "en-US")

        try:
            ocr_res = asyncio.run(do_ocr())
        except Exception:
            return

        raw_lines = []
        screen_rects = []

        for line in ocr_res.lines:
            txt = line.text.strip()
            if len(txt) < 2 or not any(c.isascii() and c.isalpha() for c in txt):
                continue

            words = line.words
            min_x = min(wd.bounding_rect.x for wd in words)
            min_y = min(wd.bounding_rect.y for wd in words)
            max_x = max(wd.bounding_rect.x + wd.bounding_rect.width for wd in words)
            max_y = max(wd.bounding_rect.y + wd.bounding_rect.height for wd in words)

            abs_x = int(rect.x() + min_x)
            abs_y = int(rect.y() + min_y)
            abs_w = int(max_x - min_x)
            abs_h = int(max_y - min_y)

            raw_lines.append(txt)
            screen_rects.append((abs_x, abs_y, abs_w, abs_h))

        if not raw_lines:
            return

        translated_lines = batch_translate(raw_lines, sl="en", tl="ru")

        blocks = []
        for r, orig, trans in zip(screen_rects, raw_lines, translated_lines):
            if trans and trans.strip():
                blocks.append({
                    "rect": r,
                    "orig": orig,
                    "trans": trans
                })

        self.hud.set_blocks(blocks)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False) # Работает всегда в фоне, даже если окна закрыты!
    controller = AppController(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
