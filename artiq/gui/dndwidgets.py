from PyQt5 import QtCore, QtWidgets


class DragDropSplitter(QtWidgets.QSplitter):
    def __init__(self, parent):
        QtWidgets.QSplitter.__init__(self, parent=parent)
        self.setAcceptDrops(True)
        self.setContentsMargins(0, 0, 0, 0)
        self.setOrientation(QtCore.Qt.Vertical)
        self.setChildrenCollapsible(False)

    def resetSizes(self):
        self.setSizes(self.count() * [1])

    def dragEnterEvent(self, e):
        e.accept()

    def dragLeaveEvent(self, e):
        self.setRubberBand(-1)
        e.accept()

    def dragMoveEvent(self, e):
        pos = e.pos()
        for n in range(self.count()):
            w = self.widget(n)
            if self.orientation() == QtCore.Qt.Vertical:
                pos_p, w_p, w_s = pos.y(), w.y(), w.size().height()
            else:
                pos_p, w_p, w_s = pos.x(), w.x(), w.size().width()
            if pos_p < w_p + w_s // 2:
                self.setRubberBand(w_p)
                break
            elif n == self.count() - 1:
                self.setRubberBand(w_p + w_s)
        e.accept()

    def dropEvent(self, e):
        pos = e.pos()
        widget = e.source()
        index = self.indexOf(widget)
        self.setRubberBand(-1)
        for n in range(self.count()):
            w = self.widget(n)
            if n <= index:
                k = n
            else:
                k = n - 1
            if self.orientation() == QtCore.Qt.Vertical:
                pos_p, w_p, w_s = pos.y(), w.y(), w.size().height()
            else:
                pos_p, w_p, w_s = pos.x(), w.x(), w.size().width()
            if pos_p < w_p + w_s // 2:
                self.insertWidget(k, widget)
                break
            elif n == self.count() - 1:
                self.insertWidget(-1, widget)
        e.accept()


# Scroll area with auto-scroll on vertical drag
class VDragScrollArea(QtWidgets.QScrollArea):
    def __init__(self, parent):
        QtWidgets.QScrollArea.__init__(self, parent)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.installEventFilter(self)
        self._margin = 40
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._on_auto_scroll)
        self._direction = 0
        self._speed = 10

    def eventFilter(self, obj, e):
        if e.type() == QtCore.QEvent.DragMove:
            val = self.verticalScrollBar().value()
            vp_height = self.viewport().height()
            pos = e.pos()
            if pos.y() < val + self._margin:
                self._direction = -1
            elif pos.y() > vp_height + val - self._margin:
                self._direction = 1
            else:
                self._direction = 0
            if not self._timer.isActive():
                self._timer.start()
        elif e.type() in (QtCore.QEvent.Drop, QtCore.QEvent.DragLeave):
            self._timer.stop()
        return False

    def setAutoScrollMargin(self, margin):
        self._margin = margin

    def _on_auto_scroll(self):
        val = self.verticalScrollBar().value()
        mini = self.verticalScrollBar().minimum()
        maxi = self.verticalScrollBar().maximum()
        dx = self._direction * self._speed
        new_val = min(maxi, max(mini, val + dx))
        self.verticalScrollBar().setValue(new_val)

