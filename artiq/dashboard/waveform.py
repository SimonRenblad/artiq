from PyQt5 import QtCore, QtWidgets, QtGui
from artiq.gui.tools import LayoutWidget
import numpy as np
import pyqtgraph as pg
import collections
import math

import logging

logger = logging.getLogger(__name__)

class WaveformScene(QtWidgets.QGraphicsScene):
    def __init__(self, parent=None):
        QtWidgets.QGraphicsScene.__init__(self, parent)
        self.width, self.height = 100, 100
        self.setSceneRect(0, 0, 100, 100)
        self.setBackgroundBrush(QtCore.Qt.black)
        
        self.channels = []

        self.data = collections.defaultdict(lambda: collections.defaultdict(list))

        self.data = {"main_channel": {0: [(0, 0), (1, 10), (0, 20)], 1: [(0, 0), (1, 20), (0, 40)]}} 

        self.x_scale, self.y_scale, self.row_scale = 100, 10, 2
        self.x_offset, self.y_offset = 0, 30

        self.timescale_unit = "ps"
        self.timescale = 1

        self.start_time = 0
        self.end_time = 40

        self.refresh_display()

        self.left_mouse_pressed = False


    def display_scale(self):
        pen = QtGui.QPen()
        pen.setStyle(QtCore.Qt.SolidLine)
        pen.setWidth(1)
        pen.setBrush(QtCore.Qt.blue)

        font = QtGui.QFont("Monospace", pointSize=7)

        print(self.timescale)
        nearest_base = (int(math.log10(self.timescale)) // 3) * 3

        divisor = math.pow(10, nearest_base)

        display_time = int(self.timescale / divisor)

        top_band_height = 16

        small_tick_height = 8

        num_small_tick = 10 # should be factor of x_scale
        adjusted_start_time = int(self.start_time / divisor)
        adjusted_end_time = int(self.end_time / divisor)
        x_start = adjusted_start_time*self.x_scale+self.x_offset
        x_end = adjusted_end_time*self.x_scale+self.x_offset

        self.addLine(x_start, top_band_height, x_end, top_band_height, pen)
        
        time = adjusted_start_time
        for x in range(x_start, x_end, self.x_scale):  
            for x_sub in range(x, x + self.x_scale, self.x_scale // num_small_tick):
               self.addLine(x_sub, top_band_height, x_sub, top_band_height + small_tick_height, pen) 
            self.addLine(x, 0, x, len(self.channels)*self.row_scale*self.y_scale + 100, pen)
            txt = self.addText(str(time) + " " + self.timescale_unit, font)
            txt.setPos(x, 0)
            txt.setDefaultTextColor(QtGui.QColor(QtCore.Qt.blue))
            time += display_time
        
        self.addLine(x_end, 0, x_end, len(self.channels)*self.row_scale*self.y_scale + 100, pen)
        txt = self.addText(str(time) + " " + self.timescale_unit, font)
        txt.setPos(x_end, 0)
        txt.setDefaultTextColor(QtGui.QColor(QtCore.Qt.blue))
    
    # Override
    def wheelEvent(self, event):
        temp = self.x_offset
        self.x_offset += int(event.delta())
        if self.x_offset > self.start_time*self.x_scale or self.x_offset < self.end_time*self.x_scale*-1:
            self.x_offset = temp
        self.refresh_display()

    def refresh_display(self):
        self.clear()
        self.display_scale()
        self.display_graph()

    def update_channels(self, channels):
        self.channels = channels
        self.refresh_display()

    def load_dump(self, dump):
        for message in dump.messages:
            self.channels.add(message.channel)
            self.data[message.channel][message.address].append((message.data, message.timestamp))

    def display_graph(self):
        row = 0
        for channel in self.channels:
            row = self._display_channel(channel, row)

    def _display_channel(self, channel, row):
        for address_data in self.data[channel].values():
            self._display_wire(address_data, row) 
            row += 1
        return row
        
    def _display_wire(self, address_data, row):
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
        rx1 = x1*self.x_scale/self.timescale + self.x_offset
        rx2 = x2*self.x_scale/self.timescale + self.x_offset
        ry1 = y1*self.y_scale + row*self.row_scale*self.y_scale + self.y_offset
        ry2 = y2*self.y_scale + row*self.row_scale*self.y_scale + self.y_offset
        return (rx1, ry1, rx2, ry2)

    def increase_timescale(self):
        self.timescale *= 5
        self.refresh_display()

    def decrease_timescale(self):
        self.timescale = max(int(self.timescale / 5), 1)
        self.refresh_display()

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


class AddChannelDialog(QtWidgets.QDialog):
    add_channel_signal = QtCore.pyqtSignal(str)

    def __init__(self, parent):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setWindowTitle("Add channel")   

        self.channels = parent.channels
        grid = QtWidgets.QGridLayout()
        grid.setRowMinimumHeight(1, 40)
        grid.setColumnMinimumWidth(2, 60)
        self.setLayout(grid)
        self.waveform_channel_list = WaveformChannelList()
        grid.addWidget(self.waveform_channel_list, 0, 0)
        self.waveform_channel_list.add_channel_signal.connect(self.add_channel)

    def add_channel(self, name):
        self.add_channel_signal.emit(name) 
        self.close()

class WaveformActiveChannelView(QtWidgets.QTreeWidget):
    update_channels_signal = QtCore.pyqtSignal(list)

    def __init__(self):
        QtWidgets.QTreeView.__init__(self)
        self.active_channels = []
        self.channels = []
        self.setMaximumWidth(150)
        self.setHeaderLabel("Channels")
        
        self.setContextMenuPolicy(QtCore.Qt.ActionsContextMenu)
        add_channel = QtWidgets.QAction("Add channel", self)
        add_channel.triggered.connect(self.add_channel_widget)
        add_channel.setShortcut("CTRL+N")
        add_channel.setShortcutContext(QtCore.Qt.WidgetShortcut)
        self.addAction(add_channel)

        self.add_channel_dialog = AddChannelDialog(self)
        self.add_channel_dialog.add_channel_signal.connect(self.add_channel)


    def add_channel_widget(self):
        self.add_channel_dialog.open()

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

        self.zoom_in_button = QtWidgets.QPushButton()
        grid.addWidget(self.zoom_in_button, 0, 0)

        self.zoom_out_button = QtWidgets.QPushButton()
        grid.addWidget(self.zoom_out_button, 0, 1)


        self.waveform_active_channel_view = WaveformActiveChannelView()
        grid.addWidget(self.waveform_active_channel_view, 1, 0)

        self.waveform_scene = WaveformScene() 
        self.waveform_view = QtWidgets.QGraphicsView(self.waveform_scene)
        grid.addWidget(self.waveform_view, 1, 1)

        self.waveform_view.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

        self.waveform_active_channel_view.update_channels_signal.connect(self.waveform_scene.update_channels)
        
        self.zoom_in_button.clicked.connect(self.waveform_scene.decrease_timescale)
        self.zoom_out_button.clicked.connect(self.waveform_scene.increase_timescale)
