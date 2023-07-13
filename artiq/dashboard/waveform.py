from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.keepalive import async_open_connection
from artiq.gui.tools import LayoutWidget, get_open_file_name
from artiq.dashboard.vcd_parser import SimpleVCDParser
from artiq.coredevice.comm_analyzer import decode_dump, InputMessage, OutputMessage, StoppedMessage, ExceptionMessage
import numpy as np
import pyqtgraph as pg
import collections
import math
import itertools
import asyncio
import struct

import logging

logger = logging.getLogger(__name__)


class Node:
    def __init__(self, data):
        self.data = data
        self.parent = None
        self.children = []

class Tree:
    def __init__(self, root):
        self.root = Node(root)

    def insert_channel(self, channel):
        channel_node = Node(channel)
        channel_node.parent = self.root
        self.root.children.append(channel_node)

    def insert_type(self, channel, msg_type):
        for node in self.root.children:
            if node.data == channel:
                type_node = Node(msg_type)
                type_node.parent = node
                node.children.append(type_node)

class WaveformActiveChannelModel(QtCore.QAbstractItemModel):
    refreshModel = QtCore.pyqtSignal()

    def __init__(self, parent=None, channel_mgr=None):
        super().__init__(parent)
        self.channel_mgr = channel_mgr
        self.active_channels = self.channel_mgr.active_channels
        self.channel_mgr.activeChannelsChanged.connect(self.update_active_channels)
        self.beginResetModel()
        self._tree = Tree("Channels")
        for act_channel in self.active_channels:
            self._tree.insert_channel(act_channel[0])
            for typ in act_channel[1]:
                self._tree.insert_type(act_channel[0], typ)
        self.endResetModel()

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
            return item.data

    def index(self, row, column, parent=QtCore.QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QtCore.QModelIndex()
        if not parent.isValid():
            return self.createIndex(0, 0, self._tree.root)
        parent_item = parent.internalPointer()
        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index):
        if not index.isValid():
            return QtCore.QModelIndex()
        item = index.internalPointer()
        if item.parent is None:
            return QtCore.QModelIndex()
        parent_item = item.parent
        if parent_item.parent is None:
            return self.createIndex(0, 0, self._tree.root)
        row = parent_item.parent.children.index(parent_item.data)
        return self.createIndex(row, 0, self.parent_item)

    def headerData(self, section, orientation, role):
        return ["Channels"]

    def rowCount(self, index=QtCore.QModelIndex()):
        if not index.isValid():
            return 1
        item = index.internalPointer()
        if item.parent is None:
            return 1
        return len(item.parent.children)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 1

    def supportedDragActions(self):
        return Qt.MoveAction

    def supportedDropActions(self):
        return Qt.MoveAction
    
    def update_active_channels(self):
        self.beginResetModel()
        self.active_channels = self.channel_mgr.active_channels
        self._tree = Tree("Channels")
        for act_channel in self.active_channels:
            self._tree.insert_channel(act_channel[0])
            for typ in act_channel[1]:
                self._tree.insert_type(act_channel[0], typ)
        self.endResetModel()

    def emitDataChanged(self):
        self.traceDataChanged.emit(QtCore.QModelIndex(), QtCore.QModelIndex())
    
    #def mimeTypes(self):
    #    return ['application/x-qabstractitemmodeldatalist']

    #def mimeData(self, indexes):
    #    mimedata = QtCore.QMimeData()
    #    encoded_data = QtCore.QByteArray()
    #    stream = QtCore.QDataStream(encoded_data, QtCore.QIODevice.WriteOnly)
    #    for index in indexes:
    #        if index.isValid():
    #            item = index.internalPointer()
    #            id = None
    #            ord = None
    #            row = index.row()
    #            if isinstance(item, Bit):
    #                id = item.channel().id
    #                ord = item.channel().bits.index(item)
    #                row = self.channel_mgr.get_row_from_id(id)
    #            elif isinstance(item, Channel):
    #                id = item.id
    #                ord = -1
    #            stream.writeInt32(id)
    #            stream.writeInt32(row)
    #            stream.writeInt32(ord)
    #    mimedata.setData('application/x-qabstractitemmodeldatalist', encoded_data)
    #    return mimedata

    #def dropMimeData(self, mimedata, action, row, column, parent):
    #    if action == Qt.IgnoreAction:
    #        return True
    #    if not mimedata.hasFormat('application/x-qabstractitemmodeldatalist'):
    #        return False
    #    if column > 0:
    #        return False
    #    if not parent.isValid():
    #        return False
    #    if row < 0:
    #        return False
    #    encoded_data = mimedata.data('application/x-qabstractitemmodeldatalist')
    #    stream = QtCore.QDataStream(encoded_data, QtCore.QIODevice.ReadOnly)
    #    source_id = stream.readInt32() 
    #    print("source_id", source_id)
    #    source_row = stream.readInt32()
    #    print("source_row", source_row)
    #    source_ord = stream.readInt32()
    #    print("source_ord", source_ord)
    #    parent_item = parent.internalPointer()
    #    print("parent_item", parent_item)
    #    # if pos is -1, full channel move
    #    if source_ord < 0:
    #        if isinstance(parent_item, str):
    #            print("end_row", row)
    #            self.channel_mgr.move_channel(row, source_row)
    #        else: 
    #            return False
    #    else:
    #        if isinstance(parent_item, Channel):
    #            print("end_row", row)
    #            self.channel_mgr.move_bit(row, source_ord, source_row)
    #        else:
    #            return False
    #    return True


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

    def setSelectionAfterMove(self, index):
        self.selectionModel().select(index, QtCore.QItemSelectionModel.ClearAndSelect)

    def add_channel_widget(self):
        self.add_channel_dialog.open()

    def update_active_channels(self):
        self.active_channels = self.channel_mgr.active_channels


class WaveformChannelList(QtWidgets.QListWidget):
    add_channel_signal = QtCore.pyqtSignal()

    def __init__(self, channel_mgr=None):
        QtWidgets.QListWidget.__init__(self)
        self.channel_mgr = channel_mgr
        for channel in self.channel_mgr.channels:
            self.addItem(str(channel))
        self.itemDoubleClicked.connect(self.emit_add_channel)
        self.channel_mgr.traceDataChanged.connect(self.update_channels)

    def emit_add_channel(self, item):
        s = item.text()
        self.channel_mgr.add_channel(int(s))
        self.add_channel_signal.emit()

    def update_channels(self):
        self.clear()
        for channel in self.channel_mgr.channels:
            self.addItem(str(channel))


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
        self.channel_mgr.traceDataChanged.connect(self.update_channels)
        self.setSceneRect(0, 0, 1000, 1000)
        self.setBackgroundBrush(Qt.black)
        self.channels = channel_mgr.active_channels
        self.x_scale, self.y_scale, self.row_scale = 100, 100, 1.1
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
        self.yellow_pen = QtGui.QPen()
        self.yellow_pen.setStyle(Qt.SolidLine)
        self.yellow_pen.setWidth(1)
        self.yellow_pen.setBrush(Qt.yellow)
        self.marker_time = self.start_time
        self.refresh_display()

    # Override
    def mouseDoubleClickEvent(self, event):
        x = event.scenePos().x()  
        self.marker_time = int(self._inverted_transform_x(x)) # TODO better type handling
        self.refresh_display()

    def display_marker(self):
        print("marker")
        x = self._transform_x(self.marker_time)
        self.addLine(x, 0, x, 900, self.red_pen)

    def refresh_display(self):
        print("refresh_display")
        self.clear()
        self.display_graph()
        self.display_marker()
        height = self.itemsBoundingRect().height()
        left = self._transform_x(self.start_time)
        right = self._transform_x(self.end_time)
        width = right - left
        self.setSceneRect(left, 0, width, height)

    def update_channels(self):
        print("update_channels")
        self.channels = self.channel_mgr.active_channels
        self.timescale = self.channel_mgr.timescale_magnitude
        self.timescale_unit = self.channel_mgr.unit
        self.start_time = self.channel_mgr.start_time
        self.end_time = self.channel_mgr.end_time
        self.refresh_display()

    def display_graph(self):
        print("graph")
        row = 0
        print(self.channels)
        for channel in self.channels:
            row = self._display_channel(channel, row)

    # TODO: updates to display channel
    # pull only needed data from message queue with filters (minimize copying / drawing)
    # data can be prefiltered in channels, then by type
    # display more detailed with a scale for each one and being able to see the wave
    def _display_channel(self, channel, row):
        for msg_type in channel[1]:
            row = self._display_waveform(channel[0], row, msg_type)
        return row

    def _normalize(self, x, min, max):
        return (x-min)/(max-min)

    # draw the waveform based on floats
    # TODO: limit to visible + buffer
    def _display_waveform(self, channel, row, msg_type, flags=None):
        # object props:
        # min, max rtio_counter values
        # data
        # row_scale
        pen = self.green_pen
        red_pen = self.red_pen
        sub_pen = self.dark_green_pen
        blue_pen = self.blue_pen
        data = self.channel_mgr.data[channel][msg_type]
        x_range = self.channel_mgr.x_range[channel][msg_type]
        y_range = self.channel_mgr.y_range[channel][msg_type]
        x_min = x_range[0]
        x_max = x_range[1]
        y_min = y_range[0]
        y_max = y_range[1]
        
        if len(data) > 0:
            # plot bottom line
            self.addLine(*self._transform_pos(x_min, 0, x_max, 0, row), blue_pen)

            tick = 0
            for x_start in range(int(x_min) - self.timescale + 1, int(x_min) + 1):
                if x_start % self.timescale == 0:
                    tick = x_start
                    break

            while tick <= x_max:
                self.addLine(*self._transform_pos(tick, 0, tick, 1, row), blue_pen)
                tick += self.timescale

            # plot top line
            self.addLine(*self._transform_pos(x_min, 1, x_max, 1, row), blue_pen)
        
        # plot messages
        for msg in self.channel_mgr.data[channel][msg_type]:
            r = msg.rtio_counter
            d = msg.data
            d_norm = self._normalize(d, y_min, y_max)
            r_t, d_t = self._transform_x_y(r, d_norm, row)
            self.addRect(r_t, d_t, 1, 1, pen, Qt.green)

        return row + 1

    # TODO: check that there is space to draw
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

    # TODO: turn other way around
    def _transform_y(self, y):
        return (1 - y) * self.y_scale + self.y_offset

    def increase_timescale(self):
        self.timescale *= 5
        self.refresh_display()

    def decrease_timescale(self):
        if not self.timescale < 5:
            self.timescale = self.timescale // 5
        self.refresh_display()


# TODO separate out the data
class ChannelManager(QtCore.QObject):
    activeChannelsChanged = QtCore.pyqtSignal()
    expandedChannelsChanged = QtCore.pyqtSignal()
    traceDataChanged = QtCore.pyqtSignal()

    def __init__(self):
        QtCore.QObject.__init__(self) 
        self.data = dict()
        self.x_range = dict()
        self.y_range = dict()
        self.active_channels = list()
        self.channels = set()
        self.expanded_channels = set()
        self.start_time = 0
        self.end_time = 100
        self.unit = 'ps'
        self.timescale = 1
        self.timescale_magnitude = 1

    def _expanded_emit(self):
        self.expandedChannelsChanged.emit()

    def move_channel(self, dest, source):
        channel = self.active_channels[source]
        if source > dest:
            source += 1
        self.active_channels.insert(dest, channel)
        self.active_channels.pop(source)
        self.broadcast_active()

    def broadcast_active(self):
        self.activeChannelsChanged.emit()

    def broadcast_data(self):
        self.traceDataChanged.emit()

    def add_channel(self, channel):
        self.active_channels.append([channel, [0,1,2,3]])
        self.broadcast_active()
        return channel

    def remove_channel(self, id):
        self.active_channels.remove(channel)
        self.broadcast_active()
        return 

    def get_active_channels(self):
        return self.active_channels

    def set_active_channels(self, active_channels):
        self.active_channels = active_channels
        self.broadcast_active()


class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)
        self.channel_mgr = ChannelManager()
        grid = LayoutWidget()
        self.setWidget(grid)
        self.zoom_in_button = QtWidgets.QPushButton()
        self.zoom_in_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_ArrowDown))
        grid.addWidget(self.zoom_in_button, 0, 0)
        self.zoom_out_button = QtWidgets.QPushButton()
        self.zoom_out_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_ArrowUp))
        grid.addWidget(self.zoom_out_button, 0, 1)
        self.load_trace_button = QtWidgets.QPushButton("Load Trace")
        self.load_trace_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_DialogOpenButton))
        grid.addWidget(self.load_trace_button, 0, 2)
        self.save_trace_button = QtWidgets.QPushButton("Save Trace")
        self.save_trace_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_DriveFDIcon))
        grid.addWidget(self.save_trace_button, 0, 3)
        self.sync_button = QtWidgets.QPushButton("Sync")
        self.sync_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_BrowserReload))
        grid.addWidget(self.sync_button, 0, 4)
        self.start_time_edit_field = QtWidgets.QLineEdit()
        grid.addWidget(self.start_time_edit_field, 0, 5)
        self.end_time_edit_field = QtWidgets.QLineEdit()
        grid.addWidget(self.end_time_edit_field, 0, 6)
        self.waveform_active_channel_view = WaveformActiveChannelView(channel_mgr=self.channel_mgr)
        grid.addWidget(self.waveform_active_channel_view, 1, 0, colspan=2)
        self.waveform_scene = WaveformScene(channel_mgr=self.channel_mgr) 
        self.waveform_view = QtWidgets.QGraphicsView(self.waveform_scene)
        grid.addWidget(self.waveform_view, 1, 2, colspan=10)
        self.waveform_view.setAlignment(Qt.AlignTop | Qt.AlignLeft)

        self.zoom_in_button.clicked.connect(self.waveform_scene.decrease_timescale)
        self.zoom_out_button.clicked.connect(self.waveform_scene.increase_timescale)
        self.load_trace_button.clicked.connect(self._load_trace_clicked)
        self.sync_button.clicked.connect(self._sync_proxy_clicked)
        self.start_time_edit_field.editingFinished.connect(self._change_start_time)
        self.end_time_edit_field.editingFinished.connect(self._change_end_time)

    def _change_start_time(self):
        start = int(self.start_time_edit_field.text())
        self.channel_mgr.start_time = start
        self.channel_mgr.broadcast_active()

    def _change_end_time(self):
        end = int(self.end_time_edit_field.text())
        self.channel_mgr.start_time = start
        self.channel_mgr.broadcast_active()

    def _load_trace_clicked(self):
        asyncio.ensure_future(self._load_trace_task())

    def _sync_proxy_clicked(self):
        asyncio.ensure_future(self._sync_proxy_task())

    async def _sync_proxy_task(self):
        # temp assumed variables -> set up subscriber and get the host + proxy port
        self.rtio_addr = "localhost"
        self.rtio_port = 1382 # proxy for rtio
        try:
            self._reader, self._writer = await async_open_connection(
                    host=self.rtio_addr,
                    port=self.rtio_port,
                    after_idle=1,
                    interval=1,
                    max_fails=3,
                )

            try:
                self._writer.write(b"ARTIQ rtio analyzer\n")
                # self._receive_task = asyncio.ensure_future(self._receive_cr())
            except:
                self._writer.close()
                del self._reader
                del self._writer
                raise
        except asyncio.CancelledError:
            logger.info("cancelled connection to rtio analyzer")
        except:
            logger.error("failed to connect to rtio analyzer. Is artiq_rtio_proxy running?", exc_info=True)
        else:
            logger.info("ARTIQ dashboard connected to moninj (%s)",
                        self.rtio_addr)
            self._writer.write(b"\x00")
            dump = await self._reader.read()
            self._reader.close()
            decoded_dump = decode_dump(dump)
            self.messages = decoded_dump.messages
            self._parse_messages(messages)

    async def _load_trace_task(self):
        vcd = None
        try:
            filename = await get_open_file_name(
                    self,
                    "Load Raw Dump",
                    "c://",
                    "All files (*.*)")
        except asyncio.CancelledError:
            return
        try:
            with open(filename, 'rb') as f:
                dump = f.read()

            decoded_dump = decode_dump(dump)
            self._parse_messages(decoded_dump.messages)
        except:
            logger.error("Failed to parse VCD file",
                         exc_info=True)

    def _message_type(self, typ):
        if isinstance(typ, OutputMessage):
            return 0
        if isinstance(typ, InputMessage):
            return 1
        if isinstance(typ, ExceptionMessage):
            return 2
        if isinstance(typ, StoppedMessage):
            return 3
        print("invalid message type")

    def _parse_messages(self, messages):
        channels = set()
        for message in messages:
            message_type = self._message_type(message)
            channels.add(message.channel)
        self.channel_mgr.channels = channels
        data = dict()
        x_range = dict()
        y_range = dict()
        for channel in channels:
            data[channel] = {
                    0: [],
                    1: [],
                    2: [],
                    3: []
            }
            x_range[channel] = {
                    0: [float("inf"), -float("inf")],
                    1: [float("inf"), -float("inf")],
                    2: [float("inf"), -float("inf")],
                    3: [float("inf"), -float("inf")]
            }
            y_range[channel] = {
                    0: [float("inf"), -float("inf")],
                    1: [float("inf"), -float("inf")],
                    2: [float("inf"), -float("inf")],
                    3: [float("inf"), -float("inf")]
            }
        for message in messages:
            message_type = self._message_type(message)
            channel = message.channel
            data[channel][message_type].append(message)
            # handle data range min and max 
            if message.rtio_counter < x_range[channel][message_type][0]:
                x_range[channel][message_type][0] = message.rtio_counter 
            if message.rtio_counter > x_range[channel][message_type][1]:
                x_range[channel][message_type][1] = message.rtio_counter 
            if message.data < y_range[channel][message_type][0]:
                y_range[channel][message_type][0] = message.data 
            if message.data > y_range[channel][message_type][1]:
                y_range[channel][message_type][1] = message.data 

        self.channel_mgr.data = data
        self.channel_mgr.x_range = x_range
        self.channel_mgr.y_range = y_range
        self.channel_mgr.traceDataChanged.emit()
