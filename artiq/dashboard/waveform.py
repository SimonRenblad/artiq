from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from artiq.gui.tools import LayoutWidget
import numpy as np
import pyqtgraph as pg
import collections
import math
import itertools

import logging

logger = logging.getLogger(__name__)


# Modified from https://github.com/futal/simpletreemodel/blob/master/simpletreemodel.py
class TreeItem:
    id_obj = itertools.count(1)

    def __init__(self, data, num=None, parent=None):
        self.id = next(TreeItem.id_obj)
        self._item_data = data
        self._item_num = num 
        self._parent_item = parent
        self._child_items = []

    def appendChild(self, item):
        item._parent_item = self
        self._child_items.append(item)

    def child(self, row):
        return self._child_items[row]

    def children(self):
        return self._child_items

    def childCount(self):
        return len(self._child_items)

    def columnCount(self):
        return len(self._item_data)

    def data(self):
        if not self._item_data:
            return QtCore.QVariant()
        return QtCore.QVariant(self._item_data)

    def num(self):
        return self._item_num

    def setData(self, value):
        self._item_data = value

    def parent(self):
        return self._parent_item

    def row(self):
        if self._parent_item:
            return self._parent_item._child_items.index(self)
        return 0

    def insertChild(self, row, item):
        self._child_items.insert(row, item)

    def pop(self, row):
        return self._child_items.pop(row)

    def __eq__(self, other):
        if other is None:
            return False
        return self.id == other.id

class WaveformActiveChannelModel(QtCore.QAbstractItemModel):
    refreshModel = QtCore.pyqtSignal()

    def __init__(self, parent=None, channel_mgr=None):
        super().__init__(parent)

        self.channel_mgr = channel_mgr
        self.active_channels = self.channel_mgr.active_channels
        self.channel_mgr.activeChannelsChanged.connect(self.update_active_channels)
        self._root_item = "Channels"

    def rootIndex(self):
        print("root index called")
        return self.createIndex(0, 0, self._root_item)

    def flags(self, index):
        defaultFlags = QtCore.QAbstractItemModel.flags(self, index)
        if not index.isValid():
            return defaultFlags
        return Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled | Qt.ItemIsSelectable | Qt.ItemIsEnabled | defaultFlags

    def data(self, index, role):
        print("data called")
        if not index.isValid():
            return self._root_item
        item = index.internalPointer()  
        if role == Qt.DisplayRole:
            if isinstance(item, Bit):
                channel = index.parent().internalPointer()
                return channel.display_bit(item)
            elif isinstance(item, Channel):
                return item.display_name
            else:
                return item


    def index(self, row, column, parent=QtCore.QModelIndex()):
        print("index called")
        if not self.hasIndex(row, column, parent):
            return self.rootIndex()
        if not parent.isValid():
            return self.rootIndex()
        if parent == self.rootIndex():
            return self.createIndex(row, column, self.active_channels[row])
        bit_item = parent.internalPointer().bits[row]
        if bit_item:
            return self.createIndex(row, column, bit_item)
        else:
            return self.rootIndex()

    def parent(self, index):
        print("parent called")
        if not index.isValid():
            return self.rootIndex()
        item = index.internalPointer()
        if isinstance(item, Bit):
            channel = item.channel()
            row = self.active_channels.index(channel)
            return self.createIndex(row, 0, channel)
        elif isinstance(item, Channel):
            return self.rootIndex()
        else:
            return QtCore.QModelIndex()

    def headerData(self, section, orientation, role):
        return ["Channels"]

    def rowCount(self, index=QtCore.QModelIndex()):
        print("rowCount called")
        if not index.isValid():
            return len(self.active_channels)
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
                pos = None
                if isinstance(item, Bit):
                    id = item.channel().id
                    pos = item.pos()
                elif isinstance(item, Channel):
                    id = item.id
                    pos = -1
                stream.writeInt32(id)
                stream.writeInt32(pos)
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
        encoded_data = mimedata.data('application/x-qabstractitemmodeldatalist')
        stream = QtCore.QDataStream(encoded_data, QtCore.QIODevice.ReadOnly)
        source_id = stream.readInt32() 
        source_pos = stream.readInt32()
        source_item = self.channel_mgr.get_channel_from_id(source_id)
        dest_item = parent.internalPointer()

        # if pos is -1, full channel move
        # TODO, move correct rows
        if source_pos < 0:
            parent_index = self.rootIndex()
            if dest_item.id == source_id:
                return False

            source_row = self.channel_mgr.get_row_from_id(source_id)
            dest_row = row
            if source_row < dest_row:
                dest_row += 1
            self.beginMoveRows(parent_index, source_row, source_row, parent_index, dest_row)
            self.active_channels.insert(dest_row, source_item)
            self.active_channels.pop(source_row)
            self.endMoveRows()
        else:
            if dest_item.id != source_id:
                return False
            parent_index = parent.parent()
            source_row = source_item.get_row_from_pos(source_pos)
            dest_row = row
            if source_row < dest_row:
                dest_row += 1
            self.beginMoveRows(parent_index, source_row, source_row, parent_index, dest_row)
            dest_item.bits.insert(dest_row, Bit(source_pos, dest_item))
            dest_item.bits.pop(source_row) 
            self.endMoveRows()

        self.channel_mgr.active_channels = self.active_channels
        self.channel_mgr.broadcast()
        return True   

class WaveformActiveChannelView(QtWidgets.QTreeView):

    def __init__(self, channel_mgr):
        QtWidgets.QTreeView.__init__(self)

        self.channel_mgr = channel_mgr
        self.channel_mgr.activeChannelsChanged.connect(self.update_active_channels)
        
        self.active_channels = []
        
        self.setMaximumWidth(150)
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

    def setSelectionAfterMove(self, index):
        self.selectionModel().select(index, QtCore.QItemSelectionModel.ClearAndSelect)

    def add_channel_widget(self):
        self.add_channel_dialog.open()

    def update_active_channels(self):
        self.active_channels = self.channel_mgr.active_channels

    def collapsed(index):
        pass        

    def expanded(index):
        pass

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

        self.width, self.height = 100, 100

        self.setSceneRect(0, 0, 100, 100)
        self.setBackgroundBrush(Qt.black)
        
        self.channels = channel_mgr.active_channels

        self.x_scale, self.y_scale, self.row_scale = 100, 20, 1.1
        self.x_offset, self.y_offset = 0, 30

        self.timescale_unit = "ps"
        self.timescale = 1

        self.start_time = 0
        self.end_time = 0

        self.refresh_display()

        self.left_mouse_pressed = False

    def display_scale(self):
        pen = QtGui.QPen()
        pen.setStyle(Qt.SolidLine)
        pen.setWidth(1)
        pen.setBrush(Qt.blue)

        font = QtGui.QFont("Monospace", pointSize=7)

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
            txt = self.addText(str(t) + " " + self.timescale_unit, font)
            txt.setPos(self._transform_x(t), 0)
            txt.setDefaultTextColor(QtGui.QColor(Qt.blue))
            t += self.timescale
    
    # Override
    def wheelEvent(self, event):
        temp = self.x_offset
        self.x_offset += int(event.delta())
        if self.x_offset > self.start_time * self.x_scale or self.x_offset < self.end_time * self.x_scale * -1:
            self.x_offset = temp
        self.refresh_display()

    def refresh_display(self):
        self.clear()
        self.display_scale()
        self.display_graph()

    def update_channels(self):
        self.channels = self.channel_mgr.active_channels
        self.end_time = self.channel_mgr.get_max_time()
        self.refresh_display()

    def display_graph(self):
        row = 0
        for channel in self.channels:
            row = self._display_channel(channel, row)

    def _display_channel(self, channel, row):
        pen = QtGui.QPen()
        pen.setStyle(Qt.SolidLine)
        pen.setWidth(1)
        pen.setBrush(Qt.green)

        sub_pen = QtGui.QPen()
        sub_pen.setStyle(Qt.SolidLine)
        sub_pen.setWidth(1)
        sub_pen.setBrush(Qt.darkGreen)
       
        messages = self.channel_mgr.data[channel.name]
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
                if not channel.is_collapsed:
                    for i, c in enumerate(channel.bits):
                        curr_bit = (current_value >> c.pos()) & 1
                        new_bit = (new_value >> c.pos()) & 1
                        self.addLine(*self._transform_pos(current_t, curr_bit, new_t, curr_bit, row + i + 1), sub_pen)
                        if new_bit != curr_bit:
                            self.addLine(*self._transform_pos(new_t, 0, new_t, 1, row + i + 1), sub_pen)
                current_value = new_value
                current_t = new_t
        return row + 1 + len(channel.bits)

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


class Channel:
    channel_id = itertools.count(1)

    def __init__(self, name, size):
        self.name = name
        self.display_name = name if size == 1 else f"{name}[{size}]"
        self.size = size
        self.id = next(Channel.channel_id) 
        self.is_collapsed = False
        self.is_active = True
        self.bits = []

    def get_row_from_pos(self, pos):
        for i, bit in enumerate(self.bits):
            if bit.pos() == pos:
                return i
    
    def resetBits(self):
        self.bits = [Bit(i, self) for i in range(self.size)]

    def collapse(self):
        self.is_collapsed = True

    def expand(self):
        self.is_collapsed = False

    def display_bit(self, bit):
        return f"{self.name}[{bit.pos()}]"

class ChannelManager(QtCore.QObject):
    activeChannelsChanged = QtCore.pyqtSignal()

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

        self.zoom_in_button = QtWidgets.QPushButton("Zoom In")
        grid.addWidget(self.zoom_in_button, 0, 0)

        self.zoom_out_button = QtWidgets.QPushButton("Zoom Out")
        grid.addWidget(self.zoom_out_button, 0, 1)


        self.waveform_active_channel_view = WaveformActiveChannelView(channel_mgr=self.channel_mgr)
        grid.addWidget(self.waveform_active_channel_view, 1, 0)

        self.waveform_scene = WaveformScene(channel_mgr=self.channel_mgr) 
        self.waveform_view = QtWidgets.QGraphicsView(self.waveform_scene)
        grid.addWidget(self.waveform_view, 1, 1)

        self.waveform_view.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        
        self.zoom_in_button.clicked.connect(self.waveform_scene.decrease_timescale)
        self.zoom_out_button.clicked.connect(self.waveform_scene.increase_timescale)
