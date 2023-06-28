from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from artiq.gui.tools import LayoutWidget, get_open_file_name
import numpy as np
import pyqtgraph as pg
import collections
import math
import itertools
import asyncio

import logging

logger = logging.getLogger(__name__)


class WaveformActiveChannelModel(QtCore.QAbstractItemModel):
    refreshModel = QtCore.pyqtSignal()

    def __init__(self, parent=None, channel_mgr=None):
        super().__init__(parent)
        self.channel_mgr = channel_mgr
        self.active_channels = self.channel_mgr.active_channels
        self.channel_mgr.activeChannelsChanged.connect(self.update_active_channels)
        self.beginResetModel()
        self._root_item = "Channels"
        self.endResetModel()

    def rootIndex(self):
        return self.createIndex(0, 0, self._root_item)

    def flags(self, index):
        flags = QtCore.QAbstractItemModel.flags(self, index)
        if index.isValid():
            flags |= Qt.ItemIsEnabled | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled | Qt.ItemIsSelectable
        return flags

    def data(self, index, role):
        if not index.isValid():
            return "Invalid Index"
        if role == Qt.DisplayRole:
            item = index.internalPointer()  
            if isinstance(item, Bit):
                channel = index.parent().internalPointer()
                return channel.display_bit(item)
            elif isinstance(item, Channel):
                return item.display_name
            else:
                return item

    def index(self, row, column, parent=QtCore.QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QtCore.QModelIndex()
        if not parent.isValid():
            return self.rootIndex()
        parent_item = parent.internalPointer()
        if parent == self.rootIndex():
            return self.createIndex(row, column, self.active_channels[row])
        return self.createIndex(row, column, parent_item.bits[row])

    def parent(self, index):
        if not index.isValid():
            return QtCore.QModelIndex()
        if index == self.rootIndex():
            return QtCore.QModelIndex()
        item = index.internalPointer()
        if isinstance(item, Bit):
            channel = item.channel()
            row = self.active_channels.index(channel)
            return self.createIndex(row, 0, channel)
        else:
            return self.rootIndex()

    def headerData(self, section, orientation, role):
        return ["Channels"]

    def rowCount(self, index=QtCore.QModelIndex()):
        if not index.isValid():
            return 1
        item = index.internalPointer()
        if isinstance(item, Bit):
            return 0
        elif isinstance(item, Channel):
            return len(item.bits)
        return len(self.active_channels)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 1

    def supportedDragActions(self):
        return Qt.MoveAction

    def supportedDropActions(self):
        return Qt.MoveAction
    
    def update_active_channels(self):
        self.beginResetModel()
        self.active_channels = self.channel_mgr.active_channels
        self.endResetModel()

    def emitDataChanged(self):
        self.dataChanged.emit(QtCore.QModelIndex(), QtCore.QModelIndex())
    
    def mimeTypes(self):
        return ['application/x-qabstractitemmodeldatalist']

    def mimeData(self, indexes):
        mimedata = QtCore.QMimeData()
        encoded_data = QtCore.QByteArray()
        stream = QtCore.QDataStream(encoded_data, QtCore.QIODevice.WriteOnly)
        for index in indexes:
            if index.isValid():
                item = index.internalPointer()
                id = None
                ord = None
                row = index.row()
                if isinstance(item, Bit):
                    id = item.channel().id
                    ord = item.channel().bits.index(item)
                    row = self.channel_mgr.get_row_from_id(id)
                elif isinstance(item, Channel):
                    id = item.id
                    ord = -1
                stream.writeInt32(id)
                stream.writeInt32(row)
                stream.writeInt32(ord)
        mimedata.setData('application/x-qabstractitemmodeldatalist', encoded_data)
        return mimedata

    def dropMimeData(self, mimedata, action, row, column, parent):
        if action == Qt.IgnoreAction:
            return True
        if not mimedata.hasFormat('application/x-qabstractitemmodeldatalist'):
            return False
        if column > 0:
            return False
        if not parent.isValid():
            return False
        if row < 0:
            return False
        encoded_data = mimedata.data('application/x-qabstractitemmodeldatalist')
        stream = QtCore.QDataStream(encoded_data, QtCore.QIODevice.ReadOnly)
        source_id = stream.readInt32() 
        print("source_id", source_id)
        source_row = stream.readInt32()
        print("source_row", source_row)
        source_ord = stream.readInt32()
        print("source_ord", source_ord)
        parent_item = parent.internalPointer()
        print("parent_item", parent_item)
        # if pos is -1, full channel move
        if source_ord < 0:
            if isinstance(parent_item, str):
                print("end_row", row)
                self.channel_mgr.move_channel(row, source_row)
            else: 
                return False
        else:
            if isinstance(parent_item, Channel):
                print("end_row", row)
                self.channel_mgr.move_bit(row, source_ord, source_row)
            else:
                return False
        return True


class WaveformActiveChannelView(QtWidgets.QTreeView):

    def __init__(self, channel_mgr):
        QtWidgets.QTreeView.__init__(self)
        self.channel_mgr = channel_mgr
        self.active_channels = []
        self.setIndentation(5)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.model = WaveformActiveChannelModel(channel_mgr=self.channel_mgr)
        self.setModel(self.model)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        add_channel = QtWidgets.QAction("Add channel", self)
        add_channel.triggered.connect(self.add_channel_widget)
        add_channel.setShortcut("CTRL+N")
        add_channel.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(add_channel)
        self.add_channel_dialog = AddChannelDialog(self, channel_mgr=self.channel_mgr)
        self.channel_mgr.activeChannelsChanged.connect(self.update_active_channels)
        self.collapsed.connect(self.channel_mgr.collapse_channel)
        self.expanded.connect(self.channel_mgr.expand_channel)

    def setSelectionAfterMove(self, index):
        self.selectionModel().select(index, QtCore.QItemSelectionModel.ClearAndSelect)

    def add_channel_widget(self):
        self.add_channel_dialog.open()

    def update_active_channels(self):
        self.active_channels = self.channel_mgr.active_channels
        self.setExpanded(self.model.rootIndex(), True)
        for id in self.channel_mgr.expanded_channels:
            row = self.channel_mgr.get_row_from_id(id)
            index = self.model.index(row, 0, self.model.rootIndex())
            self.setExpanded(index, True)


class WaveformChannelList(QtWidgets.QListWidget):
    add_channel_signal = QtCore.pyqtSignal()

    def __init__(self, channel_mgr=None):
        QtWidgets.QListWidget.__init__(self)
        self.channel_mgr = channel_mgr
        for channel in self.channel_mgr.channels:
            self.addItem(channel)
        self.itemDoubleClicked.connect(self.emit_add_channel)

    def emit_add_channel(self, item):
        s = item.text()
        self.channel_mgr.add_channel_by_name(s)
        self.add_channel_signal.emit()


class AddChannelDialog(QtWidgets.QDialog):

    def __init__(self, parent, channel_mgr=None):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setWindowTitle("Add channel")   
        self.channel_mgr = channel_mgr
        grid = QtWidgets.QGridLayout()
        grid.setRowMinimumHeight(1, 40)
        grid.setColumnMinimumWidth(2, 60)
        self.setLayout(grid)
        self.waveform_channel_list = WaveformChannelList(self.channel_mgr)
        grid.addWidget(self.waveform_channel_list, 0, 0)
        self.waveform_channel_list.add_channel_signal.connect(self.add_channel)

    def add_channel(self):
        self.close()


class WaveformScene(QtWidgets.QGraphicsScene):
    def __init__(self, parent=None, channel_mgr=None):
        QtWidgets.QGraphicsScene.__init__(self, parent)
        self.channel_mgr = channel_mgr
        self.channel_mgr.activeChannelsChanged.connect(self.update_channels)
        self.channel_mgr.expandedChannelsChanged.connect(self.update_channels)
        self.width, self.height = 100, 100
        self.setSceneRect(0, 0, 1000, 1000)
        self.setBackgroundBrush(Qt.black)
        self.channels = channel_mgr.active_channels
        self.x_scale, self.y_scale, self.row_scale = 100, 20, 1.1
        self.x_offset, self.y_offset = 0, 30
        self.timescale_unit = "ps"
        self.timescale = 1
        self.start_time = 0
        self.end_time = 0
        self.left_mouse_pressed = False
        self.blue_pen = QtGui.QPen()
        self.blue_pen.setStyle(Qt.SolidLine)
        self.blue_pen.setWidth(1)
        self.blue_pen.setBrush(Qt.blue)
        self.font = QtGui.QFont("Monospace", pointSize=7)
        self.green_pen = QtGui.QPen()
        self.green_pen.setStyle(Qt.SolidLine)
        self.green_pen.setWidth(1)
        self.green_pen.setBrush(Qt.green)
        self.red_pen = QtGui.QPen()
        self.red_pen.setStyle(Qt.SolidLine)
        self.red_pen.setWidth(1)
        self.red_pen.setBrush(Qt.red)
        self.dark_green_pen = QtGui.QPen()
        self.dark_green_pen.setStyle(Qt.SolidLine)
        self.dark_green_pen.setWidth(1)
        self.dark_green_pen.setBrush(Qt.darkGreen)
        self.marker_time = self.start_time
        self.refresh_display()

    def display_scale(self):
        pen = self.blue_pen
        top_band_height = 16
        small_tick_height = 8
        vert_line_extra_length = 500
        num_small_tick = 10 # should be factor of x_scale
        x_start = self._transform_x(self.start_time)
        x_end = self._transform_x(self.end_time)
        self.addLine(x_start, top_band_height, x_end, top_band_height, pen)
        t = self.start_time
        while t < self.end_time:
            self.addLine(self._transform_x(t), 0, self._transform_x(t), len(self.channels)*self.row_scale*self.y_scale + vert_line_extra_length, pen)
            txt = self.addText(str(t) + " " + self.timescale_unit, self.font)
            txt.setPos(self._transform_x(t), 0)
            txt.setDefaultTextColor(QtGui.QColor(Qt.blue))
            t += self.timescale
    
    # Override
    def wheelEvent(self, event):
        temp = self.x_offset
        self.x_offset += int(event.delta())
        if self.x_offset > self.start_time * self.x_scale or self.x_offset < self.end_time * self.x_scale * -1:
            self.x_offset = temp
        event.accept()
        self.refresh_display()
    
    # Override
    def mouseDoubleClickEvent(self, event):
        x = event.scenePos().x()  
        self.marker_time = int(self._inverted_transform_x(x)) # TODO better type handling
        self.refresh_display()

    def display_marker(self):
        x = self._transform_x(self.marker_time)
        self.addLine(x, 0, x, 900, self.red_pen)

    def refresh_display(self):
        self.clear()
        self.display_scale()
        self.display_graph()
        self.display_marker()
        self.setSceneRect(self.itemsBoundingRect())

    def update_channels(self):
        self.channels = self.channel_mgr.active_channels
        self.end_time = self.channel_mgr.get_max_time()
        self.refresh_display()

    def display_graph(self):
        row = 0
        for channel in self.channels:
            row = self._display_channel(channel, row)

    def _display_channel(self, channel, row):
        pen = self.green_pen
        sub_pen = self.dark_green_pen
        messages = self.channel_mgr.data[channel.name]
        expd_channels = self.channel_mgr.expanded_channels
        bits = channel.bits if channel.id in expd_channels else []
        current_value = messages[0][0]
        current_t = messages[0][1]
        self._draw_value(current_t, current_value, row)
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
                self._draw_value(new_t, new_value, row)
                for i, c in enumerate(bits):
                    curr_bit = (current_value >> c.pos()) & 1
                    new_bit = (new_value >> c.pos()) & 1
                    self.addLine(*self._transform_pos(current_t, curr_bit, new_t, curr_bit, row + i + 1), sub_pen)
                    if new_bit != curr_bit:
                        self.addLine(*self._transform_pos(new_t, 0, new_t, 1, row + i + 1), sub_pen)
                current_value = new_value
                current_t = new_t
        return row + 1 + len(bits)

    def _draw_value(self, time, value, row):
        x, y = self._transform_x_y(time, 1, row)
        txt = self.addText(str(value), self.font)
        txt.setPos(x, y)
        txt.setDefaultTextColor(QtGui.QColor(Qt.white))

    def _transform_pos(self, x1, y1, x2, y2, row):
        rx1 = self._transform_x(x1)
        rx2 = self._transform_x(x2)
        ry1 = self._transform_y(y1) + row * self.row_scale * self.y_scale
        ry2 = self._transform_y(y2) + row * self.row_scale * self.y_scale
        return (rx1, ry1, rx2, ry2)

    def _transform_x_y(self, x, y, row):
        rx = self._transform_x(x)
        ry = self._transform_y(y) + row * self.row_scale * self.y_scale
        return rx, ry

    def _transform_x(self, x):
        return x * self.x_scale / self.timescale + self.x_offset

    def _inverted_transform_x(self, x):
        return (x - self.x_offset) * self.timescale / self.x_scale

    def _transform_y(self, y):
        return (1 - y) * self.y_scale + self.y_offset

    def increase_timescale(self):
        self.timescale *= 5
        self.refresh_display()

    def decrease_timescale(self):
        if not self.timescale < 5:
            self.timescale = self.timescale // 5
        self.refresh_display()


class Bit:
    def __init__(self, position, parent):
        self.parent = parent
        self.position = position

    def pos(self):
        return self.position

    def channel(self):
        return self.parent

    def __repr__(self):
        return str(self.pos())


class Channel:
    channel_id = itertools.count(1)

    def __init__(self, name, size):
        self.name = name
        self.display_name = name if size == 1 else f"{name}[{size}]"
        self.size = size
        self.id = next(Channel.channel_id) 
        self.bits = []

    def get_row_from_pos(self, pos):
        for i, bit in enumerate(self.bits):
            if bit.pos() == pos:
                return i
    
    def resetBits(self):
        self.bits = [Bit(i, self) for i in range(self.size)]

    def display_bit(self, bit):
        return f"{self.name}[{bit.pos()}]"

    def __repr__(self):
        return self.display_name + ": " + str(self.bits)

class ChannelManager(QtCore.QObject):
    activeChannelsChanged = QtCore.pyqtSignal()
    expandedChannelsChanged = QtCore.pyqtSignal()

    def __init__(self):
        QtCore.QObject.__init__(self) 
        self.data = {
            "main_channel": [(0,0),(1,10),(0,20)],
            "side_channel": [(0,0),(1,10),(0,20)],
        }
        self.size = {
            "main_channel": 8,
            "side_channel": 8,
        }
        self.active_channels = []
        self.channels = ["main_channel", "side_channel"]
        self.expanded_channels = set()

    def expand_channel(self, index):
        if not index.isValid():
            return
        item = index.internalPointer()
        if isinstance(item, Channel):
            id = index.internalPointer().id
            self.expanded_channels.add(id)
        self._expanded_emit()

    def collapse_channel(self, index):
        if not index.isValid():
            return
        item = index.internalPointer()
        if isinstance(item, Channel):
            id = index.internalPointer().id
            self.expanded_channels.remove(id)
        self._expanded_emit()

    def _expanded_emit(self):
        self.expandedChannelsChanged.emit()

    def move_channel(self, dest, source):
        channel = self.active_channels[source]
        if source > dest:
            source += 1
        self.active_channels.insert(dest, channel)
        self.active_channels.pop(source)
        self.broadcast()

    def move_bit(self, dest, source, index):
        channel = self.active_channels[index]
        bit = channel.bits[source]
        if source > dest:
            source += 1
        channel.bits.insert(dest, bit)
        channel.bits.pop(source)
        self.active_channels[index] = channel
        self.broadcast()

    def broadcast(self):
        self.activeChannelsChanged.emit()

    def get_max_time(self):
        max_time = 0
        for channel in self.active_channels:
            channel_max = self.data[channel.name][-1][1]
            max_time = max(max_time, channel_max)
        return max_time
    
    def add_channel(self, channel):
        self.active_channels.append(channel)
        self.broadcast()
        return channel.id

    def add_channel_by_name(self, name):
        channel = Channel(name, self.size[name])
        channel.resetBits()
        self.active_channels.append(channel)
        self.broadcast()
        return channel.id

    def remove_channel(self, id):
        for channel in self.active_channels:
            if channel.id == id:
                self.active_channels.remove(channel)
                self.broadcast()
                return 

    def get_active_channels(self):
        return self.active_channels

    def set_active_channels(self, active_channels):
        self.active_channels = active_channels
        self.broadcast()

    def get_channel_from_id(self, id):
        for channel in self.active_channels:
            if channel.id == id:
                return channel

    def get_row_from_id(self, id):
        for i, c in enumerate(self.active_channels):
            if c.id == id:
                return i
                
    def get_id_from_row(self, row):
        return self.active_channels[row]

    def collapse_row(self, row):
        self.active_channels[row].collapse()
        self.broadcast()

    def expand_row(self, row):
        self.active_channels[row].expand()
        self.broadcast()

    def get_data_from_id(self, id):
        for channel in self.active_channels:
            if channel.id == id:
                return self.data[channel.name]

    def load_data(self):
        pass


class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)
        self.channel_mgr = ChannelManager()
        grid = LayoutWidget()
        self.setWidget(grid)
        self.zoom_in_button = QtWidgets.QPushButton("+")
        grid.addWidget(self.zoom_in_button, 0, 0)
        self.zoom_out_button = QtWidgets.QPushButton("-")
        grid.addWidget(self.zoom_out_button, 0, 1)
        self.load_trace_button = QtWidgets.QPushButton("Load Trace")
        grid.addWidget(self.load_trace_button, 0, 2)
        self.save_trace_button = QtWidgets.QPushButton("Save Trace")
        grid.addWidget(self.save_trace_button, 0, 3)
        self.sync_button = QtWidgets.QPushButton("Sync")
        grid.addWidget(self.sync_button, 0, 10)
        self.waveform_active_channel_view = WaveformActiveChannelView(channel_mgr=self.channel_mgr)
        grid.addWidget(self.waveform_active_channel_view, 1, 0, colspan=2)
        self.waveform_scene = WaveformScene(channel_mgr=self.channel_mgr) 
        self.waveform_view = QtWidgets.QGraphicsView(self.waveform_scene)
        grid.addWidget(self.waveform_view, 1, 2, colspan=10)
        self.waveform_view.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.zoom_in_button.clicked.connect(self.waveform_scene.decrease_timescale)
        self.zoom_out_button.clicked.connect(self.waveform_scene.increase_timescale)
        self.load_trace_button.clicked.connect(self._load_trace_clicked)

    def _load_trace_clicked(self):
        asyncio.ensure_future(self._load_trace_task())

    async def _load_trace_task(self):
        try:
            filename = await get_open_file_name(
                    self,
                    "Load Trace",
                    "c://",
                    "VCD files (*.vcd);;All files (*.*)")
        except asyncio.CancelledError:
            return

        #try:
        #    with open(filename, "r") as f:
        #        pass
        print(name)
