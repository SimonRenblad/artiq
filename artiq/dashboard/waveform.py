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

    def __init__(self, data, parent=None):
        self.id = next(TreeItem.id_obj)
        self._item_data = data
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
    moveFinished = QtCore.pyqtSignal(QtCore.QModelIndex)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.active_channels = {} 
        self._root_item = TreeItem('Channel')
        self.beginInsertRows(QtCore.QModelIndex(), 0, 3)
        item1 = TreeItem('1')
        item2 = TreeItem('2')
        item3 = TreeItem('3')
        item4 = TreeItem('4')
        self._root_item.appendChild(item1)
        self._root_item.appendChild(item2)
        self._root_item.appendChild(item3)
        self._root_item.appendChild(item4)
        self.endInsertRows()

    def flags(self, index):
        defaultFlags = QtCore.QAbstractItemModel.flags(self, index)
        if not index.isValid():
            return defaultFlags
        return QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsDragEnabled | QtCore.Qt.ItemIsDropEnabled | QtCore.Qt.ItemIsEditable | QtCore.Qt.ItemIsSelectable | QtCore.Qt.ItemIsEnabled | defaultFlags

    def data(self, index, role):
        if not index.isValid():
            return QtCore.QVariant()
        item = index.internalPointer()  
        if role == Qt.DisplayRole:
            return QtCore.QVariant(item.data())

    def index(self, row, column, parent=QtCore.QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QtCore.QModelIndex()
        if not parent.isValid():
            parent_item = self._root_item
        else:
            parent_item = parent.internalPointer()
        child_item = parent_item.child(row)
        if child_item:
            return self.createIndex(row, column, child_item)
        else:
            return QtCore.QModelIndex()

    def parent(self, index):
       if not index.isValid():
          return QtCore.QModelIndex()
       child_item = index.internalPointer()
       if not child_item:
          return QtCore.QModelIndex()
       parent_item = child_item.parent()
       if parent_item == self._root_item:
          return QtCore.QModelIndex()
       if not parent_item:
           return QtCore.QModelIndex()
       return self.createIndex(parent_item.row(), 0, parent_item)

    def headerData(self, section, orientation, role):
        return ["Channels"]

    def rowCount(self, parent=QtCore.QModelIndex()):
        if parent.isValid():
            return parent.internalPointer().childCount()
        return self._root_item.childCount()

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 1

    def supportedDragActions(self):
        return Qt.MoveAction

    def supportedDropActions(self):
        return Qt.MoveAction

    def addChannel(self, channel):
        row = self.rowCount()
        self.beginInsertRows(QtCore.QModelIndex(), row, row)
        self._root_item.appendChild(TreeItem(channel))
        self.endInsertRows()

    def setData(self, index, value, role):
        if not index.isValid():
            return
        item = index.internalPointer()  
        item.setData(value)
        return True

    def setItemData(self, index, roles):
        if not index.isValid():
            return
        item = index.internalPointer()
        if Qt.DisplayRole in roles.keys():
            item.setData(roles[Qt.DisplayRole])
    
    def mimeTypes(self):
        return ['application/x-qabstractitemmodeldatalist']

    def mimeData(self, indexes):
        mimedata = QtCore.QMimeData()
        encoded_data = QtCore.QByteArray()
        stream = QtCore.QDataStream(encoded_data, QtCore.QIODevice.WriteOnly)
        for index in indexes:
            if index.isValid():
                item = index.internalPointer()
                stream.writeInt32(item.id)
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
        dest_item = parent.internalPointer()
        parent_item = dest_item.parent()
        encoded_data = mimedata.data('application/x-qabstractitemmodeldatalist')
        stream = QtCore.QDataStream(encoded_data, QtCore.QIODevice.ReadOnly)
        source_id = stream.readInt32() 
        source_item = None
        for item in parent_item.children():
            if item.id == source_id:
                source_item = item
                break
        if source_item is None:
            return False

        if source_item == dest_item:
            return False
        
        source_row = int(source_item.row())
        dest_row = int(dest_item.row())
        target_row = dest_row
        if dest_row > source_row:
            target_row += 1
        else:
            source_row += 1
        self.beginMoveRows(parent.parent(), target_row, target_row, parent.parent(), source_row)
        parent_item.insertChild(target_row, source_item)
        parent_item.pop(source_row) 
        self.endMoveRows()
        self.moveFinished.emit(source_item)
        return True   

class WaveformActiveChannelView(QtWidgets.QTreeView):
    update_active_channels_signal = QtCore.pyqtSignal(list)

    def __init__(self):
        QtWidgets.QTreeView.__init__(self)
        self.active_channels = []
        self.channels = ["main_channel"]
        self.channel_size_map = {"main_channel": 8}
        self.setMaximumWidth(150)
        self.setIndentation(5)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.model = WaveformActiveChannelModel()
        self.setModel(self.model)
        self.model.moveFinished.emit(self.setSelectionAfterMove)
        
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        add_channel = QtWidgets.QAction("Add channel", self)
        add_channel.triggered.connect(self.add_channel_widget)
        add_channel.setShortcut("CTRL+N")
        add_channel.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(add_channel)

        self.add_channel_dialog = AddChannelDialog(self)
        self.add_channel_dialog.add_channel_signal.connect(self.add_active_channel)

    def setSelectionAfterMove(self, index):
        self.selectionModel().select(index, QtCore.QItemSelectionModel.ClearAndSelect)

    def add_channel_widget(self):
        self.add_channel_dialog.open()

    def add_active_channel(self, name):
        self.model.addChannel(name)

    def update_active_channels(self):
        self.update_active_channels_signal.emit(self.active_channels)



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


class WaveformScene(QtWidgets.QGraphicsScene):
    def __init__(self, parent=None):
        QtWidgets.QGraphicsScene.__init__(self, parent)
        self.width, self.height = 100, 100

        self.setSceneRect(0, 0, 100, 100)
        self.setBackgroundBrush(Qt.black)
        
        self.channels = []

        self.data = {"main_channel":  [(10, 0), (0, 10), (14, 20)]}

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
        if self.x_offset > self.start_time*self.x_scale or self.x_offset < self.end_time*self.x_scale*-1:
            self.x_offset = temp
        self.refresh_display()

    def refresh_display(self):
        self.clear()
        self.display_scale()
        self.display_graph()

    def update_channels(self, channels):
        self.channels = channels
        self.end_time = self._get_max_time()
        self.refresh_display()

    def _get_max_time(self):
        max_time = 0
        for channel, messages in self.data.items():
            if channel in [x[0] for x in self.channels]:
               new_time = messages[-1][1]
               if new_time > max_time:
                   max_time = new_time
        return max_time

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
        pen.setStyle(Qt.SolidLine)
        pen.setWidth(1)
        pen.setBrush(Qt.green)

        sub_pen = QtGui.QPen()
        sub_pen.setStyle(Qt.SolidLine)
        sub_pen.setWidth(1)
        sub_pen.setBrush(Qt.darkGreen)
       
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

        self.waveform_view.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self.waveform_active_channel_view.update_active_channels_signal.connect(self.waveform_scene.update_channels)
        
        self.zoom_in_button.clicked.connect(self.waveform_scene.decrease_timescale)
        self.zoom_out_button.clicked.connect(self.waveform_scene.increase_timescale)
