from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.keepalive import async_open_connection
from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient
from artiq.gui.tools import LayoutWidget, get_open_file_name, get_save_file_name
from artiq.coredevice.comm_analyzer import decode_dump
import numpy as np
import pyqtgraph as pg
import collections
import math
import itertools
import asyncio
import struct
from enum import Enum
import time
import atexit
from artiq.tools import exc_to_warning

import logging

logger = logging.getLogger(__name__)

class MessageType(Enum):
    OutputMessage = 0
    InputMessage = 1
    ExceptionMessage = 2
    StoppedMessage = 3

class DisplayType(Enum):
    INT_64 = 0
    FLOAT_64 = 1

class ActiveChannelList(QtWidgets.QListWidget):

    def __init__(self, channel_mgr):
        QtWidgets.QListWidget.__init__(self)
        self.cmgr = channel_mgr
        self.active_channels = []
        
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QtWidgets.QAbstractItemView.InternalMove)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
       
        # Add channel
        self.add_channel_dialog = AddChannelDialog(self, channel_mgr=self.cmgr)
        add_channel = QtWidgets.QAction("Add channel...", self)
        add_channel.triggered.connect(lambda: self.add_channel_dialog.open())
        add_channel.setShortcut("CTRL+N")
        add_channel.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(add_channel)

        # Remove channel
        remove_channel = QtWidgets.QAction("Delete", self)
        remove_channel.triggered.connect(self.remove_channel)
        self.addAction(remove_channel)

        # Data format
        data_format_menu = QtWidgets.QMenu("Data Format", self)
        int_format = QtWidgets.QAction("Int", self)
        float_format = QtWidgets.QAction("Float", self)
        data_format_menu.addAction(int_format)
        data_format_menu.addAction(float_format)
        data_format_action = QtWidgets.QAction("Data Format", self)
        data_format_action.setMenu(data_format_menu)
        self.addAction(data_format_action)
        int_format.triggered.connect(lambda: self.set_waveform_datatype(DisplayType.INT_64))
        float_format.triggered.connect(lambda: self.set_waveform_datatype(DisplayType.FLOAT_64))

        # Message type to display
        message_type_action = QtWidgets.QAction("Filter message types...", self)
        self.addAction(message_type_action)
        message_type_action.triggered.connect(self.display_message_type_filter)

        self.cmgr.traceDataChanged.connect(self.clear)
        # Save list 
#        save_list_action = QtWidgets.QAction("Save active list", self)
#        self.addAction(save_list_action)
#        save_list_action.triggered.connect(self.save_current_list)

#    def save_current_list(self):
#        l = self.channel_mgr.active_channels
#
        # save in some cache or save as file... not sure yet
    def _selected_channel(self):
        item = self.currentItem()
        s = item.text()
        c = self.cmgr.channel_name_id_map.get_by_left(s)
        return item, c

    def remove_channel(self):
        item, channel = self._selected_channel()
        self.takeItem(self.row(item))
        self.cmgr.active_channels.remove(channel)
        self.cmgr.broadcast_active()

    def display_message_type_filter(self):
        item, channel = self._selected_channel()
        dialog = MessageTypeFilterDialog(self, 
                                         channel_mgr=self.cmgr,
                                         channel=channel)
        dialog.open()

    def set_waveform_datatype(self, ty):
        item, channel = self._selected_channel()
        self.cmgr.display_types[channel] = ty
        self.cmgr.broadcast_active()

    def add_channel(self, channel):
        self.addItem(channel)
        channel = self.cmgr.channel_name_id_map.get_by_left(channel)
        self.cmgr.active_channels.append(channel)
        self.cmgr.broadcast_active()

class MessageTypeFilterDialog(QtWidgets.QDialog):
    def __init__(self, parent, channel_mgr=None, channel=None):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setWindowTitle("Filter message types")
        self.cmgr = channel_mgr
        self.channel = channel
        layout = QtWidgets.QVBoxLayout()
        self.b0 = QtWidgets.QCheckBox("OutputMessage") 
        self.b1 = QtWidgets.QCheckBox("InputMessage") 
        self.b2 = QtWidgets.QCheckBox("ExceptionMessage")
        msg_types = self.cmgr.msg_types.get(self.channel, set())
        if 0 in msg_types:
            self.b0.setChecked(True)
        if 1 in msg_types:
            self.b1.setChecked(True)
        if 2 in msg_types:
            self.b2.setChecked(True)
        self.confirm = QtWidgets.QPushButton("Confirm")
        layout.addWidget(self.b0)
        layout.addWidget(self.b1)
        layout.addWidget(self.b2)
        layout.addWidget(self.confirm)
        self.confirm.clicked.connect(self.confirm_filter)
        self.setLayout(layout)
    
    def confirm_filter(self):
        self.cmgr.msg_types[self.channel] = set()
        if self.b0.isChecked():
            self.cmgr.msg_types[self.channel].add(MessageType.OutputMessage)
        if self.b1.isChecked():
            self.cmgr.msg_types[self.channel].add(MessageType.InputMessage)
        if self.b2.isChecked():
            self.cmgr.msg_types[self.channel].add(MessageType.ExceptionMessage)
        self.cmgr.broadcast_active()
        self.close()

class AddChannelDialog(QtWidgets.QDialog):

    def __init__(self, parent, channel_mgr=None):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setWindowTitle("Add channel")   
        self.cmgr = channel_mgr
        self.parent = parent
        grid = QtWidgets.QGridLayout()
        grid.setRowMinimumHeight(1, 40)
        grid.setColumnMinimumWidth(2, 60)
        self.setLayout(grid)
        self.waveform_channel_list = QtWidgets.QListWidget()
        grid.addWidget(self.waveform_channel_list, 0, 0)
        self.waveform_channel_list.itemDoubleClicked.connect(self.add_channel)
        self.cmgr.traceDataChanged.connect(self.update_channels)

    def add_channel(self, channel):
        self.parent.add_channel(channel.text())
        self.close()

    def update_channels(self):
        self.waveform_channel_list.clear()
        for channel in self.cmgr.channels:
            name = self.cmgr.channel_name_id_map.get_by_right(channel)
            self.waveform_channel_list.addItem(name)


class WaveformWidget(pg.PlotWidget):
    mouseMoved = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None, channel_mgr=None):
        pg.PlotWidget.__init__(self, parent=parent)
        self.addLegend()
        self.showGrid(True, True, 0.5)
        self.setLabel('bottom', text='time', units='s')
        self.cmgr = channel_mgr
        self.cmgr.activeChannelsChanged.connect(self.refresh_display)
        self.cmgr.traceDataChanged.connect(self.refresh_display)
        self.plots = list()
        self.left_mouse_pressed = False
        self.refresh_display()
        self.proxy = pg.SignalProxy(self.scene().sigMouseMoved, rateLimit=60, slot=self.get_cursor_coordinates)

    def get_cursor_coordinates(self, event):
        mousePoint = self.getPlotItem().vb.mapSceneToView(event[0])
        self.mouseMoved.emit(mousePoint.x(), mousePoint.y())
    
    def refresh_display(self):
        start = time.monotonic()
        self.display_graph()
        end = time.monotonic()
        logger.info(f"Refresh took {(end - start)*1000} ms")

    def display_graph(self):
        # redraw with each update - not expecting frequent updates
        for plot in self.plots:
            self.removeItem(plot)
        self.plots = list()

        for channel in self.cmgr.active_channels:
            for msg_type in self.cmgr.msg_types[channel]:
                self._display_waveform(channel, MessageType(msg_type))
    
    @staticmethod
    def convert_type(data, display_type):
        if display_type == DisplayType.INT_64:
            return data
        if display_type == DisplayType.FLOAT_64:
            return struct.unpack('>d', struct.pack('>Q', data))[0]

    def _display_waveform(self, channel, msg_type):
        display_type = self.cmgr.display_types.get(channel, DisplayType.INT_64)
        data = self.cmgr.data[channel].get(msg_type.value, [])
        if len(data) == 0:
            return
        x_data = np.zeros(len(data))
        y_data = np.zeros(len(data))
        for i, x in enumerate(data):
            x_data[i] = x.rtio_counter

        pen = None
        symbol = None
        if msg_type in [MessageType.OutputMessage, MessageType.InputMessage]:
            for i, y in enumerate(data):
                y_data[i] = self.convert_type(y.data, display_type)
            pen = {'color': msg_type.value, 'width': 1}
        else:
            symbol = 'x'

        pdi = self.plot(x_data,
                        y_data,
                        symbol=symbol,
                        name=f"Channel: {channel}, {msg_type.name}",
                        pen=pen)
        self.plots.append(pdi)
        return

# convenience class
class BijectiveMap:
    def __init__(self):
        self._ltor = dict()
        self._rtol = dict()

    def add(self, left, right):
        if left in self._ltor:
            del self._rtol[self._ltor[left]]
        if right in self._rtol:
            del self._ltor[self._rtol[right]]
        self._ltor[left] = right
        self._rtol[right] = left

    def get_by_left(self, left, default=None):
        return self._ltor.get(left, default)

    def get_by_right(self, right, default=None):
        return self._rtol.get(right, default)

    def lefts(self):
        return self._ltor.keys()

    def rights(self):
        return self._rtol.keys()

class _ChannelManager(QtCore.QObject):
    activeChannelsChanged = QtCore.pyqtSignal()
    traceDataChanged = QtCore.pyqtSignal()

    def __init__(self):
        QtCore.QObject.__init__(self) 
        self.data = dict()
        self.active_channels = list()
        self.channel_name_id_map = BijectiveMap()
        self.msg_types = dict()
        self.channels = set()
        self.display_types = dict()
        self.start_time = 0
        self.end_time = 100
        self.unit = 'ps'
        self.timescale = 1
        self.timescale_magnitude = 1

    def broadcast_active(self):
        self.activeChannelsChanged.emit()

    def broadcast_data(self):
        self.traceDataChanged.emit()

    def add_channel(self, channel):
        self.active_channels.append(channel)
        self.broadcast_active()

    def remove_channel(self, id):
        self.active_channels.remove(channel)
        self.broadcast_active()

    def get_active_channels(self):
        return self.active_channels

    def set_active_channels(self, active_channels):
        self.active_channels = active_channels
        self.broadcast_active()


class _TraceManager:
    def __init__(self, parent, channel_mgr):
        self.parent = parent
        self.cmgr = channel_mgr
        self.rtio_addr = None
        self.rtio_port = None
        self.rtio_port_control = None
        self.dump = None
        self.subscriber = Subscriber("devices", self.init_ddb, self.update_ddb)
        self.proxy_client = AsyncioClient()
        self.trace_subscriber = Subscriber("rtio_trace", self.init_dump, self.update_dump) 
        self.proxy_reconnect = asyncio.Event()

    # parsing and loading dump
    @staticmethod
    def _parse_messages(messages):
        channels = set()
        data = dict()
        msg_types = dict()
        for message in messages:
            # get class name directly to avoid name conflict with comm_analyzer.MessageType
            message_type = MessageType[message.__class__.__name__]
            if message_type == MessageType.StoppedMessage:
                break

            c = message.channel
            v = message_type.value

            msg_types.setdefault(c, set())
            data.setdefault(c, {})
            data[c].setdefault(v, [])

            channels.add(c)
            msg_types[c].add(v)
            data[c][v].append(message)
        return channels, data, msg_types

    def _update_from_dump(self, dump):
        self.dump = dump
        decoded_dump = decode_dump(dump)
        messages = decoded_dump.messages

        channels, data, msg_types = self._parse_messages(messages)

        # default names if not defined in devicedb
        for c in channels:
            if c not in self.cmgr.channel_name_id_map.rights():
                self.cmgr.channel_name_id_map.add("unnamed_channel"+str(c), c)

        self.cmgr.channels = channels
        self.cmgr.data = data
        self.cmgr.msg_types = msg_types
        self.cmgr.active_channels = list()
        self.cmgr.traceDataChanged.emit()

    async def _load_trace_task(self):
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
            self._update_from_dump(dump)
        except:
            logger.error("Failed to parse binary trace file",
                         exc_info=True)

    async def _save_trace_task(self):
        try:
            filename = await get_save_file_name(
                    self,
                    "Save Raw Dump",
                    "c://",
                    "All files (*.*)")
        except asyncio.CancelledError:
            return
        try:
            with open(filename, 'wb') as f:
                f.write(self.dump)
        except:
            logger.error("Failed to save binary trace file",
                         exc_info=True)

    def init_dump(self, dump):
        return dump

    def update_dump(self, mod):
        dump = mod.get("value", None)
        if dump:
            logger.info("calling update from dump")
            self._update_from_dump(dump)
    
    @staticmethod 
    def _sent_bytes_from_header(header):
        if header[0] == ord('E'):
            endian = '>'
        elif header[0] == ord('e'):
            endian = '<'
        else:
            raise ValueError
        return struct.unpack(endian + "I", header[1:5])[0]

    async def _pull_from_device_task(self):
        try:
            logger.info("attempt pull from device")
            asyncio.ensure_future(exc_to_warning(self.proxy_client.pull_from_device()))
        except:
            logger.error("Pull from device failed, is proxy running?", exc_info=1)
    
    # change to a loop that waits on proxy_reconnect
    async def start(self, server, port):
        try:
            await self.subscriber.connect(server, port)
            await self.proxy_reconnect.wait()
            await self.proxy_client.connect_rpc(self.rtio_addr, self.rtio_port_control, "rtio_proxy_control")
            await self.trace_subscriber.connect(self.rtio_addr, self.rtio_port)
        except TimeoutError:
            logger.error("Time out", exc_info=1)
        except:
            logger.error("other exception", exc_info=1)
        finally:
            self.proxy_reconnect.clear()

    async def stop(self):
        try:
            await self.subscriber.close()
            self.proxy_client.close_rpc()
            await self.trace_subscriber.close()
        except:
            logger.error("Error closing proxy connections", exc_info=1)

    def init_ddb(self, ddb):
        self.ddb = ddb

    def update_ddb(self, mod):
        channel_name_id_map = BijectiveMap()
        for name, desc in self.ddb.items():
            if isinstance(desc, dict):
                if "arguments" in desc and "channel" in desc["arguments"] and desc["type"] == "local":
                    channel = desc["arguments"]["channel"]
                    print(name, channel)
                    channel_name_id_map.add(name, channel)
                elif desc["type"] == "controller" and name == "core_analyzer":
                    self.rtio_addr = desc["host"]
                    self.rtio_port = desc.get("port_proxy", 1382)
                    self.rtio_port_control = desc.get("port_proxy_control", 1385)
        self.cmgr.channel_name_id_map = channel_name_id_map
        if self.rtio_addr is not None:
            self.proxy_reconnect.set()

    # handler for ccb
    def ccb_notify(self, message):
        try:
            service = message["service"]
            if service == "pull_trace_from_device":
                asyncio.ensure_future(exc_to_warning(self.proxy_client.pull_from_device()))
        except:
            logger.error("failed to process CCB", exc_info=True)

class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)

        self.cmgr = _ChannelManager()
        self.tm = _TraceManager(parent=self, channel_mgr=self.cmgr)

        grid = LayoutWidget()
        self.setWidget(grid)

        self.load_trace_button = QtWidgets.QPushButton("Load Trace")
        self.load_trace_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_DialogOpenButton))
        grid.addWidget(self.load_trace_button, 0, 0)
        self.load_trace_button.clicked.connect(self._load_trace_clicked)

        self.save_trace_button = QtWidgets.QPushButton("Save Trace")
        self.save_trace_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_DriveFDIcon))
        grid.addWidget(self.save_trace_button, 0, 1)
        self.save_trace_button.clicked.connect(self._save_trace_clicked)

        self.pull_button = QtWidgets.QPushButton("Pull from device buffer")
        self.pull_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_ArrowUp))
        grid.addWidget(self.pull_button, 0, 3)
        self.pull_button.clicked.connect(self._pull_from_device_clicked)

        self.coord_label = QtWidgets.QLabel("x: y: ")
        grid.addWidget(self.coord_label, 1, 2, colspan=10)
        
        self.waveform_active_channel_view = ActiveChannelList(channel_mgr=self.cmgr)
        grid.addWidget(self.waveform_active_channel_view, 2, 0, colspan=2)
        self.waveform_widget = WaveformWidget(channel_mgr=self.cmgr) 
        grid.addWidget(self.waveform_widget, 2, 2, colspan=10)
        self.waveform_widget.mouseMoved.connect(self.update_coord_label)


    def update_coord_label(self, coord_x, coord_y):
        self.coord_label.setText(f"x: {coord_x} y: {coord_y}")

    # load from binary file
    def _load_trace_clicked(self):
        asyncio.ensure_future(self.tm._load_trace_task())

    # save to binary file
    def _save_trace_clicked(self):
        asyncio.ensure_future(self.tm._save_trace_task())
    
    # pull data from device buffer
    def _pull_from_device_clicked(self):
        asyncio.ensure_future(self.tm._pull_from_device_task())


