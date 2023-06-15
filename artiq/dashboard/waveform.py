from PyQt5 import QtCore, QtWidgets, QtGui
from artiq.gui.tools import LayoutWidget
import numpy as np
import pyqtgraph as pg
import collections

import logging

logger = logging.getLogger(__name__)

class WaveformScene(QtWidgets.QGraphicsScene):
    def __init__(self, parent=None):
        QtWidgets.QGraphicsScene.__init__(self, parent)
        self.setSceneRect(0, 0, 100, 100)
        self.setBackgroundBrush(QtCore.Qt.black)
        
        self.channels = set()

        self.data = collections.defaultdict(lambda: collections.defaultdict(list))

        self.data = {0: {0: [(0, 0), (1, 10), (0, 20)], 1: [(0, 0), (1, 20), (0, 40)]}} 
        self.channels.add(0)

        self.x_scale, self.y_scale, self.row_scale = 10, 10, 2
        self.x_offset, self.y_offset = 0, 0

        self.display_graph()

        self.left_mouse_pressed = False

    # # Override
    # def mousePressEvent(self, event):
    #     if event.button() == QtCore.Qt.LeftButton:
    #         self.left_mouse_pressed = True
    #         self.pan_start_x = event.scenePos().x()
    #         self.pan_start_y = event.scenePos().y()

    # # Override
    # def mouseReleaseEvent(self, event):
    #     if event.button() == QtCore.Qt.LeftButton:
    #         self.left_mouse_pressed = False

    # # Override
    # def mouseMoveEvent(self, event):
    #     if self.left_mouse_pressed:
    #         x = event.scenePos().x()
    #         y = event.scenePos().y()
    #         self.x_offset += x - self.pan_start_x
    #         self.y_offset += y - self.pan_start_y
    #         self.pan_start_x = x
    #         self.pan_start_y = y
    #         self.clear()
    #         self.display_graph()


    def load_dump(self, dump):
        for message in dump.messages:
            self.channels.add(message.channel)
            self.data[message.channel][message.address].append((message.data, message.timestamp))

    def display_graph(self):
        for channel in sorted(list(self.channels)):
            self._display_channel(channel)

    def _display_channel(self, channel):
        for row, address_data in enumerate(self.data[channel].values()):
            self._display_address(address_data, row) 
        
    def _display_address(self, address_data, row):
        pen = QtGui.QPen()
        pen.setStyle(QtCore.Qt.SolidLine)
        pen.setWidth(1)
        pen.setBrush(QtCore.Qt.green)
        current_value = address_data[0][0]
        current_stamp = address_data[0][1]
        for i in range(len(address_data)):
            if current_value != address_data[i][0]:
                self.addLine(*self._transform_pos(current_stamp, current_value, address_data[i][1], current_value, row), pen)
                self.addLine(*self._transform_pos(address_data[i][1], current_value, address_data[i][1], address_data[i][0], row), pen)
                current_value = address_data[i][0]
                current_stamp = address_data[i][1]

    def _transform_pos(self, x1, y1, x2, y2, row):
        rx1 = x1*self.x_scale + self.x_offset
        rx2 = x2*self.x_scale + self.x_offset
        ry1 = y1*self.y_scale + row*self.row_scale*self.y_scale + self.y_offset
        ry2 = y2*self.y_scale + row*self.row_scale*self.y_scale + self.y_offset
        return (rx1, ry1, rx2, ry2)

class WaveformChannelList(QtWidgets.QListWidget):
    def __init__(self):
        QtWidgets.QListWidget.__init__(self)

        self.channels = ["ab", "bc", "de"]
        model = QtGui.QStandardItemModel()
        for channel in self.channels:
            model.appendRow(QtWidgets.QStandardItem(channel))

        self.itemDoubleClicked.connect(self.double_clicked)

    def double_clicked(self, item):
        print(item.text())

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

        # self.waveform_widget = _WaveformWidget()
        # grid.addWidget(self.waveform_widget, 1, 0)

        self.waveform_scene = WaveformScene() 

        self.waveform_channel_list = WaveformChannelList()

        grid.addWidget(self.waveform_channel_list, 1, 0)

        self.waveform_view = QtWidgets.QGraphicsView(self.waveform_scene)
        grid.addWidget(self.waveform_view, 1, 1)



# class _WaveformWidget(pg.PlotWidget):
#     def __init__(self):
#         pg.PlotWidget.__init__(self)
#         self.text = pg.TextItem("")
#         self.x = np.asarray([1,2,3,4])
#         self.y = np.asarray([0,1,1,0])
#         self.plot(self.x, self.y)
