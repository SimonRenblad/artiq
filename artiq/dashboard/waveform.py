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
from enum import Enum

import logging

logger = logging.getLogger(__name__)


class Node:
    def __init__(self, data, display_name=None):
        self.data = data
        self.parent = None
        self.children = []
        self.display_name = display_name if display_name else data

    def print_node(self):
        print(self.data)
        for c in self.children:
            c.print_node()

class Tree:
    def __init__(self, root):
        self.root = Node(root)

    def insert_channel(self, channel):
        channel_node = Node(channel, f"Channel: {channel}")
        channel_node.parent = self.root
        self.root.children.append(channel_node)

    def insert_type(self, channel, msg_type):
        for node in self.root.children:
            if node.data == channel:
                type_node = Node(msg_type, 
                                 message_type_string(msg_type))
                type_node.parent = node
                node.children.append(type_node)

    def print_tree(self):
        self.root.print_node()

def message_type_string(type_as_int):
    if type_as_int == 0:
        return "OutputMessage"
    if type_as_int == 1:
        return "InputMessage"
    if type_as_int == 2:
        return "ExceptionMessage"
    if type_as_int == 3:
        return "StoppedMessage"

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
            return item.display_name

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
        row = parent_item.parent.children.index(parent_item)
        return self.createIndex(row, 0, parent_item)

    def headerData(self, section, orientation, role):
        return ["Channels"]

    def rowCount(self, index=QtCore.QModelIndex()):
        if not index.isValid():
            return 1
        item = index.internalPointer()
        return len(item.children)

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


class WaveformActiveChannelView(QtWidgets.QTreeView):

    def __init__(self, channel_mgr):
        QtWidgets.QTreeView.__init__(self)
        self.channel_mgr = channel_mgr
        self.active_channels = []
        self.setIndentation(5)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        #self.setItemsExpandable(False)
        #self.setRootIsDecorated(False)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.model = WaveformActiveChannelModel(channel_mgr=self.channel_mgr)
        self.setModel(self.model)
        self.model.modelReset.connect(lambda x: self.expandAll())
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        add_channel = QtWidgets.QAction("Add channel", self)
        add_channel.triggered.connect(self.add_channel_widget)
        add_channel.setShortcut("CTRL+N")
        add_channel.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(add_channel)

        self.data_format_menu = QtWidgets.QMenu("Data Format")
        self.int_format = QtWidgets.QAction("Int")
        self.float_format = QtWidgets.QAction("Float")
        self.data_format_menu.addAction(self.int_format)
        self.data_format_menu.addAction(self.float_format)

        self.data_format_action = QtWidgets.QAction("Data Format")
        self.data_format_action.setMenu(self.data_format_menu)
        self.addAction(self.data_format_action)

        self.int_format.triggered.connect(self.set_int_format)
        self.float_format.triggered.connect(self.set_float_format)

        self.add_channel_dialog = AddChannelDialog(self, channel_mgr=self.channel_mgr)
        self.channel_mgr.activeChannelsChanged.connect(self.update_active_channels)

    def set_format(self, ty):
        index = self.selectionModel().selectedIndexes()[0]
        item = index.internalPointer()
        if not item.children:
            msg_type = item.data
            channel = item.parent.data
            self.channel_mgr.display_types[channel][msg_type] = ty
            self.channel_mgr.expandedChannelsChanged.emit()


    def set_int_format(self):
        self.set_format(DisplayType.INT_64)

    def set_float_format(self):
        self.set_format(DisplayType.FLOAT_64)

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

class DisplayType(Enum):
    INT_64 = 0
    FLOAT_64 = 1


class WaveformWidget(pg.PlotWidget):
    def __init__(self, parent=None, channel_mgr=None):
        pg.PlotWidget.__init__(self, parent=parent)
        #self.vbox = QtWidgets.QVBoxLayout()
        self.addLegend()
        self.showGrid(True, True, 0.5)
        self.channel_mgr = channel_mgr
        self.channel_mgr.activeChannelsChanged.connect(self.update_channels)
        self.channel_mgr.expandedChannelsChanged.connect(self.update_channels)
        self.channel_mgr.traceDataChanged.connect(self.update_channels)
        self.channels = channel_mgr.active_channels
        self.display_types = dict()
        self.channel_plots = list()
        self.timescale_unit = "ps"
        self.timescale = 1
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
        self.refresh_display()

    def refresh_display(self):
        print("refresh_display")
        self.display_graph()

    def update_channels(self):
        print("update_channels")
        self.channels = self.channel_mgr.active_channels
        self.timescale = self.channel_mgr.timescale_magnitude
        self.timescale_unit = self.channel_mgr.unit
        self.display_types = self.channel_mgr.display_types
        print(self.display_types)
        self.refresh_display()

    def display_graph(self):
        print("graph")
        row = 0
        print(self.channels)
        for channel in self.channels:
            row = self._display_channel(channel, row)

    def _display_channel(self, channel, row):
        for msg_type in channel[1]:
            row = self._display_waveform(channel[0], row, msg_type)
        return row
    
    @staticmethod
    def convert_type(data, display_type):
        if display_type == DisplayType.INT_64:
            return data
        if display_type == DisplayType.FLOAT_64:
            return struct.unpack('>d', struct.pack('>Q', data))[0]

    def _display_waveform(self, channel, row, msg_type, display_type=DisplayType.INT_64):
        pen = self.green_pen
        red_pen = self.red_pen
        sub_pen = self.dark_green_pen
        blue_pen = self.blue_pen
        data = self.channel_mgr.data[channel][msg_type]
        display_type = self.display_types[channel].get(msg_type, DisplayType.INT_64)
        if len(data) == 0:
            return row
        x_data = [x.rtio_counter for x in data]
        y_data = [self.convert_type(y.data, display_type) for y in data]

        if row < len(self.channel_plots):
            self.channel_plots[row].setData(x_data, y_data)
        else:
            pdi = self.plot(x_data, 
                            y_data, 
                            name=f"Channel: {channel}, Type: {msg_type}",
                            pen={'color': msg_type, 'width': 1})
            self.channel_plots.append(pdi)
        return row + 1

class ChannelManager(QtCore.QObject):
    activeChannelsChanged = QtCore.pyqtSignal()
    expandedChannelsChanged = QtCore.pyqtSignal()
    traceDataChanged = QtCore.pyqtSignal()

    def __init__(self):
        QtCore.QObject.__init__(self) 
        self.data = dict()
        self.active_channels = list()
        self.channels = set()
        self.expanded_channels = set()
        self.display_types = dict()
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
        self.waveform_widget = WaveformWidget(channel_mgr=self.channel_mgr) 
        grid.addWidget(self.waveform_widget, 1, 2, colspan=10)
        self.load_trace_button.clicked.connect(self._load_trace_clicked)
        self.sync_button.clicked.connect(self._sync_proxy_clicked)
        self.start_time_edit_field.editingFinished.connect(self._change_start_time)
        self.end_time_edit_field.editingFinished.connect(self._change_end_time)

    def ccb_notify(self, message):
        try:
            service = message["service"]
            if service == "show_trace":
                asyncio.ensure_future(self._sync_proxy_task())
        except:
            logger.error("failed to process CCB", exc_info=True)

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
            logger.info("ARTIQ dashboard connected to rtio analyzer (%s)",
                        self.rtio_addr)
            self._writer.write(b"\x00")
            dump = await self._reader.read()
            decoded_dump = decode_dump(dump)
            self.messages = decoded_dump.messages
            self._parse_messages(self.messages)

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
        display_types = dict()
        for channel in channels:
            data[channel] = {
                    0: [],
                    1: [],
                    2: [],
                    3: []
            }
            display_types[channel] = {}
        for message in messages:
            message_type = self._message_type(message)
            channel = message.channel
            data[channel][message_type].append(message)

        self.channel_mgr.data = data
        self.channel_mgr.display_types = display_types
        self.channel_mgr.traceDataChanged.emit()
