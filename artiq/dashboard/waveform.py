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

        self.data = {"main_channel":  [(10, 0), (0, 10), (14, 20)]}

        self.x_scale, self.y_scale, self.row_scale = 100, 20, 1.1
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

        top_band_height = 16

        small_tick_height = 8

        num_small_tick = 10 # should be factor of x_scale
        x_start = self._transform_x(self.start_time)
        x_end = self._transform_x(self.end_time)

        self.addLine(x_start, top_band_height, x_end, top_band_height, pen)
        
        t = self.start_time
        while t < self.end_time:
            self.addLine(self._transform_x(t), 0, self._transform_x(t), len(self.channels)*self.row_scale*self.y_scale + 100, pen)
            txt = self.addText(str(t) + " " + self.timescale_unit, font)
            txt.setPos(self._transform_x(t), 0)
            txt.setDefaultTextColor(QtGui.QColor(QtCore.Qt.blue))
            t += self.timescale
    
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
            row = self._display_channel(channel[0], channel[1:], row)

    def _display_channel(self, channel, subchannels, row):
        pen = QtGui.QPen()
        pen.setStyle(QtCore.Qt.SolidLine)
        pen.setWidth(1)
        pen.setBrush(QtCore.Qt.green)

        sub_pen = QtGui.QPen()
        sub_pen.setStyle(QtCore.Qt.SolidLine)
        sub_pen.setWidth(1)
        sub_pen.setBrush(QtCore.Qt.darkGreen)
       
        messages = self.data[channel]
        current_value = messages[0][0]
        current_t = messages[0][1]
        for i in range(len(messages)):
            new_value = messages[i][0] 
            new_t = messages[i][1]
            if current_value != new_value:
                if current_value > 1:
                    self.addLine(*self._transform_pos(current_t, 0, new_t, 0, row), pen)
                    self.addLine(*self._transform_pos(current_t, 1, new_t, 1, row), pen)
                    self.addLine(*self._transform_pos(new_t, 0, new_t, 1, row), pen)
                else:
                    self.addLine(*self._transform_pos(current_t, current_value, new_t, current_value, row), pen)
                    self.addLine(*self._transform_pos(new_t, current_value, new_t, min(new_value, 1), row), pen)
                for i, c in enumerate(subchannels):
                    curr_bit = (current_value >> c) & 1
                    new_bit = (new_value >> c) & 1
                    self.addLine(*self._transform_pos(current_t, curr_bit, new_t, curr_bit, row + i + 1), sub_pen)
                    if new_bit != curr_bit:
                        self.addLine(*self._transform_pos(new_t, 0, new_t, 1, row + i + 1), sub_pen)
                current_value = new_value
                current_t = new_t
        return row + 1 + len(subchannels)

    def _transform_pos(self, x1, y1, x2, y2, row):
        rx1 = self._transform_x(x1)
        rx2 = self._transform_x(x2)
        ry1 = self._transform_y(y1) + row * self.row_scale * self.y_scale
        ry2 = self._transform_y(y2) + row * self.row_scale * self.y_scale
        return (rx1, ry1, rx2, ry2)

    def _transform_x(self, x):
        return x * self.x_scale / self.timescale + self.x_offset

    def _transform_y(self, y):
        return y * self.y_scale + self.y_offset

    def increase_timescale(self):
        self.timescale *= 5
        self.x_offset = self.x_offset // 5
        self.refresh_display()

    def decrease_timescale(self):
        if not self.timescale < 5:
            self.timescale = self.timescale // 5
            self.x_offset = self.x_offset * 5
        self.refresh_display()

class WaveformChannelList(QtWidgets.QListWidget):
    add_channel_signal = QtCore.pyqtSignal(str)

    def __init__(self):
        QtWidgets.QListWidget.__init__(self)

        self.channels = {"main_channel": [0, 1]}
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
    update_active_channels_signal = QtCore.pyqtSignal(list)

    def __init__(self):
        QtWidgets.QTreeView.__init__(self)
        self.active_channels = []
        self.channels = {"main_channel": [0, 1]}
        self.setMaximumWidth(150)
        self.setHeaderLabel("Channels")
        self.setIndentation(5)
        
        self.setContextMenuPolicy(QtCore.Qt.ActionsContextMenu)
        add_channel = QtWidgets.QAction("Add channel", self)
        add_channel.triggered.connect(self.add_channel_widget)
        add_channel.setShortcut("CTRL+N")
        add_channel.setShortcutContext(QtCore.Qt.WidgetShortcut)
        self.addAction(add_channel)

        self.add_channel_dialog = AddChannelDialog(self)
        self.add_channel_dialog.add_channel_signal.connect(self.add_active_channel)

    def add_channel_widget(self):
        self.add_channel_dialog.open()

    def add_active_channel(self, name):
        item = QtWidgets.QTreeWidgetItem([name])
        self.active_channels.append([name] + [x for x in self.channels[name]])
        for sbc in self.channels[name]:
            child = QtWidgets.QTreeWidgetItem([name + "[" + str(sbc) + "]"])
            item.addChild(child)
        self.addTopLevelItem(item)
        self.update_active_channels()

    def update_active_channels(self):
        self.update_active_channels_signal.emit(self.active_channels)


class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)
        
        grid = LayoutWidget()
        self.setWidget(grid)

        self.zoom_in_button = QtWidgets.QPushButton("Zoom In")
        grid.addWidget(self.zoom_in_button, 0, 0)

        self.zoom_out_button = QtWidgets.QPushButton("Zoom Out")
        grid.addWidget(self.zoom_out_button, 0, 1)


        self.waveform_active_channel_view = WaveformActiveChannelView()
        grid.addWidget(self.waveform_active_channel_view, 1, 0)

        self.waveform_scene = WaveformScene() 
        self.waveform_view = QtWidgets.QGraphicsView(self.waveform_scene)
        grid.addWidget(self.waveform_view, 1, 1)

        self.waveform_view.setAlignment(QtCore.Qt.AlignTop | QtCore.Qt.AlignLeft)

        self.waveform_active_channel_view.update_active_channels_signal.connect(self.waveform_scene.update_channels)
        
        self.zoom_in_button.clicked.connect(self.waveform_scene.decrease_timescale)
        self.zoom_out_button.clicked.connect(self.waveform_scene.increase_timescale)
