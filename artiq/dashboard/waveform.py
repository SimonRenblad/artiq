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
        
        self.channels = []

        self.data = collections.defaultdict(lambda: collections.defaultdict(list))

        self.data = {"main_channel": {0: [(0, 0), (1, 10), (0, 20)], 1: [(0, 0), (1, 20), (0, 40)]}} 

        self.x_scale, self.y_scale, self.row_scale = 10, 10, 2
        self.x_offset, self.y_offset = 0, 0 

        self.display_graph()

        self.left_mouse_pressed = False
    
    # Override
    def wheelEvent(self, event):
        self.x_offset += event.delta()
        self.refresh_display()

    def refresh_display(self):
        self.clear()
        self.display_graph()

    def update_channels(self, channels):
        self.channels = channels
        self.refresh_display()

    def load_dump(self, dump):
        for message in dump.messages:
            self.channels.add(message.channel)
            self.data[message.channel][message.address].append((message.data, message.timestamp))

    def display_graph(self):
        for channel in self.channels:
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
    add_channel_signal = QtCore.pyqtSignal(str)

    def __init__(self):
        QtWidgets.QListWidget.__init__(self)

        self.channels = ["main_channel"]
        for channel in self.channels:
            self.addItem(channel)

        self.itemDoubleClicked.connect(self.emit_add_channel)

    def emit_add_channel(self, item):
        s = item.text()
        self.add_channel_signal.emit(s)

class WaveformActiveChannelView(QtWidgets.QTreeWidget):
    update_channels_signal = QtCore.pyqtSignal(list)

    def __init__(self):
        QtWidgets.QTreeView.__init__(self)
        self.channels = []

    def add_channel(self, name):
        item = QtWidgets.QTreeWidgetItem([name])
        self.channels.append(name)
        self.addTopLevelItem(item)
        self.update_channels()

    def update_channels(self):
        self.update_channels_signal.emit(self.channels)
        

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

        self.waveform_channel_list = WaveformChannelList()
        grid.addWidget(self.waveform_channel_list, 1, 0)

        self.waveform_active_channel_view = WaveformActiveChannelView()
        grid.addWidget(self.waveform_active_channel_view, 2, 0)

        self.waveform_scene = WaveformScene() 
        self.waveform_view = QtWidgets.QGraphicsView(self.waveform_scene)
        grid.addWidget(self.waveform_view, 1, 1, rowspan=2)

        self.waveform_view.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

        self.waveform_channel_list.add_channel_signal.connect(self.waveform_active_channel_view.add_channel)
        self.waveform_active_channel_view.update_channels_signal.connect(self.waveform_scene.update_channels)
