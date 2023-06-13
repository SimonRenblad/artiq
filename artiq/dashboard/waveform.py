from PyQt5 import QtCore, QtWidgets, QtGui
from artiq.gui.tools import LayoutWidget
import numpy as np
import pyqtgraph as pg

import logging

logger = logging.getLogger(__name__)

class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)
        
        grid = LayoutWidget()
        self.setWidget(grid)
        
        self.temp_label = QtWidgets.QLabel("temporary label")
        grid.addWidget(self.temp_label, 0, 0)

        self.waveform_widget = _WaveformWidget()
        grid.addWidget(self.waveform_widget, 1, 0)

class _WaveformWidget(pg.PlotWidget):
    def __init__(self):
        pg.PlotWidget.__init__(self)
        self.text = pg.TextItem("")
        self.x = np.zeros(4)
        self.plot(self.x)
