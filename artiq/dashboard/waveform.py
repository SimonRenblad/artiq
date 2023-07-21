from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.keepalive import async_open_connection
from sipyco.sync_struct import Subscriber
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

def message_type_string(type_as_int):
    if type_as_int == 0:
        return "OutputMessage"
    if type_as_int == 1:
        return "InputMessage"
    if type_as_int == 2:
        return "ExceptionMessage"
    if type_as_int == 3:
        return "StoppedMessage"

class ActiveChannelList(QtWidgets.QListWidget):

    def __init__(self, channel_mgr):
        QtWidgets.QListWidget.__init__(self)
        self.channel_mgr = channel_mgr
        self.active_channels = []
        
        self.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
       
        # Add channel
        add_channel = QtWidgets.QAction("Add channel...", self)
        add_channel.triggered.connect(self.add_channel_widget)
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
        int_format.triggered.connect(self.set_int_format)
        float_format.triggered.connect(self.set_float_format)

        # Message type to display
        message_type_action = QtWidgets.QAction("Filter message types...", self)
        self.addAction(message_type_action)
        message_type_action.triggered.connect(self.display_message_type_filter)

        self.add_channel_dialog = AddChannelDialog(self, channel_mgr=self.channel_mgr)

    def remove_channel(self):
        item = self.currentItem()
        channel = self.channel_mgr.id(item.text())
        self.takeItem(self.row(item))
        self.channel_mgr.active_channels.remove(channel)
        self.channel_mgr.broadcast_active()

    def display_message_type_filter(self):
        item = self.currentItem()
        channel = self.channel_mgr.id(item.text())
        dialog = MessageTypeFilterDialog(self, 
                                         channel_mgr=self.channel_mgr,
                                         channel=channel)
        dialog.open()

    def set_format(self, ty):
        item = self.currentItem()
        channel = self.channel_mgr.id(item.text())
        self.channel_mgr.display_types[channel] = ty
        self.channel_mgr.broadcast_active()

    def set_int_format(self):
        self.set_format(DisplayType.INT_64)

    def set_float_format(self):
        self.set_format(DisplayType.FLOAT_64)

    def add_channel_widget(self):
        self.add_channel_dialog.open()

    def add_channel(self, channel):
        self.addItem(channel)
        self.channel_mgr.add_channel(self.channel_mgr.id(channel))

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
        msg_types = self.cmgr.msg_types.get(self.channel, [0,1])
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
        self.cmgr.msg_types[self.channel] = list()
        if self.b0.isChecked():
            self.cmgr.msg_types[self.channel].append(0)
        if self.b1.isChecked():
            self.cmgr.msg_types[self.channel].append(1)
        if self.b2.isChecked():
            self.cmgr.msg_types[self.channel].append(2)
        self.cmgr.broadcast_active()
        self.close()

class AddChannelDialog(QtWidgets.QDialog):

    def __init__(self, parent, channel_mgr=None):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setWindowTitle("Add channel")   
        self.channel_mgr = channel_mgr
        self.parent = parent
        grid = QtWidgets.QGridLayout()
        grid.setRowMinimumHeight(1, 40)
        grid.setColumnMinimumWidth(2, 60)
        self.setLayout(grid)
        self.waveform_channel_list = QtWidgets.QListWidget()
        grid.addWidget(self.waveform_channel_list, 0, 0)
        self.waveform_channel_list.itemDoubleClicked.connect(self.add_channel)
        self.channel_mgr.traceDataChanged.connect(self.update_channels)

    def add_channel(self, channel):
        self.parent.add_channel(channel.text())
        self.close()

    def update_channels(self):
        self.waveform_channel_list.clear()
        for channel in self.channel_mgr.channels:
            self.waveform_channel_list.addItem(self.channel_mgr.name(channel))


class DisplayType(Enum):
    INT_64 = 0
    FLOAT_64 = 1

class WaveformWidget(pg.PlotWidget):
    def __init__(self, parent=None, channel_mgr=None):
        pg.PlotWidget.__init__(self, parent=parent)
        self.addLegend()
        self.showGrid(True, True, 0.5)
        self.setLabel('bottom', text='time', units='s')
        self.cmgr = channel_mgr
        self.cmgr.activeChannelsChanged.connect(self.update_channels)
        self.cmgr.traceDataChanged.connect(self.update_channels)
        self.plots = dict()
        self.left_mouse_pressed = False
        self.refresh_display()

    def refresh_display(self):
        self.display_graph()

    def update_channels(self):
        self.refresh_display()

    def display_graph(self):
        # redraw with each update - not expecting frequent updates
        for plot in self.plots.values():
            self.removeItem(plot)
        self.plots = dict()
        for channel in self.cmgr.active_channels:
            self._display_channel(channel)

    def _display_channel(self, channel):
        for msg_type in self.cmgr.msg_types.get(channel, [0,1]):
            self._display_waveform(channel, msg_type)
    
    @staticmethod
    def convert_type(data, display_type):
        if display_type == DisplayType.INT_64:
            return data
        if display_type == DisplayType.FLOAT_64:
            return struct.unpack('>d', struct.pack('>Q', data))[0]

    def _display_waveform(self, channel, msg_type):
        display_type = self.cmgr.display_types.get(channel, DisplayType.INT_64)
        data = self.cmgr.data[channel][msg_type]
        if len(data) == 0:
            return
        x_data = [x.rtio_counter for x in data]
        if msg_type < 2:
            y_data = [self.convert_type(y.data, display_type) for y in data]

            pdi = self.plot(x_data, 
                            y_data, 
                            name=f"Channel: {channel}, {message_type_string(msg_type)}",
                            pen={'color': msg_type, 'width': 1})
            self.plots[(channel, msg_type)] = pdi
        else:
            pdi = self.plot(x_data, 
                            symbol='x',
                            name=f"Channel: {channel}, {message_type_string(msg_type)}",
                            pen={'color': msg_type, 'width': 1})
            self.plots[(channel, msg_type)] = pdi
        return

class ChannelManager(QtCore.QObject):
    activeChannelsChanged = QtCore.pyqtSignal()
    traceDataChanged = QtCore.pyqtSignal()

    def __init__(self):
        QtCore.QObject.__init__(self) 
        self.data = dict()
        self.active_channels = list()
        self.channel_names = dict()
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

    def name(self, channel):
        return self.channel_names.get(channel, str(channel))

    def id(self, name):
        for k, v in self.channel_names.items():
            if v == name:
                return k
        return int(name)

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


class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.dump = None
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)
        self.channel_mgr = ChannelManager()
        grid = LayoutWidget()
        self.setWidget(grid)
        self.load_trace_button = QtWidgets.QPushButton("Load Trace")
        self.load_trace_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_DialogOpenButton))
        grid.addWidget(self.load_trace_button, 0, 0)
        self.save_trace_button = QtWidgets.QPushButton("Save Trace")
        self.save_trace_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_DriveFDIcon))
        grid.addWidget(self.save_trace_button, 0, 1)
        self.sync_button = QtWidgets.QPushButton("Sync")
        self.sync_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_BrowserReload))
        grid.addWidget(self.sync_button, 0, 2)
        self.waveform_active_channel_view = ActiveChannelList(channel_mgr=self.channel_mgr)
        grid.addWidget(self.waveform_active_channel_view, 1, 0, colspan=2)
        self.waveform_widget = WaveformWidget(channel_mgr=self.channel_mgr) 
        grid.addWidget(self.waveform_widget, 1, 2, colspan=10)
        self.load_trace_button.clicked.connect(self._load_trace_clicked)
        self.save_trace_button.clicked.connect(self._save_trace_clicked)
        self.sync_button.clicked.connect(self._sync_proxy_clicked)

        self.subscriber = Subscriber("devices", self.init_ddb, self.update_ddb)
        self._receive_task = None

    # load from binary file
    def _load_trace_clicked(self):
        asyncio.ensure_future(self._load_trace_task())

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
            self.dump = dump
            decoded_dump = decode_dump(dump)
            self._parse_messages(decoded_dump.messages)
        except:
            logger.error("Failed to parse binary trace file",
                         exc_info=True)

    # save to binary file
    def _save_trace_clicked(self):
        asyncio.ensure_future(self._save_trace_task())

    async def _save_trace_task(self):
        if dump is not None:
            pass


    # sync with proxy
    def _sync_proxy_clicked(self):
        asyncio.ensure_future(self._sync_proxy_task())

    async def _sync_proxy_task(self):
        # temp assumed variables
        self.rtio_addr = "::1" # true loop back address
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
                self._receive_task = asyncio.ensure_future(self._receive_cr())
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
            self._writer.write(b"\x00") ## make separate coroutine

    async def _receive_cr(self):
        dump = await self._reader.read()
        decoded_dump = decode_dump(dump)
        self.messages = decoded_dump.messages
        self._parse_messages(self.messages)

    # parsing of messages (TODO: make pure funcs and move to comm_analyzer)
    def _message_type(self, typ):
        if isinstance(typ, OutputMessage):
            return 0
        if isinstance(typ, InputMessage):
            return 1
        if isinstance(typ, ExceptionMessage):
            return 2
        if isinstance(typ, StoppedMessage):
            return 3

    def _parse_messages(self, messages):
        channels = set()
        for message in messages:
            if self._message_type(message) == 3:
                break
            message_type = self._message_type(message)
            channels.add(message.channel)
        self.channel_mgr.channels = channels
        data = dict()
        for channel in channels:
            data[channel] = {
                    0: [],
                    1: [],
                    2: [],
                    3: []
            }
        for message in messages:
            message_type = self._message_type(message)
            if message_type == 3:
                break
            channel = message.channel
            data[channel][message_type].append(message)

        self.channel_mgr.data = data
        self.channel_mgr.traceDataChanged.emit()
    
    # connect to devicedb
    async def start(self, server, port):
        await self.subscriber.connect(server, port)

    async def stop(self):
        await self.subscriber.close()
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await asyncio.wait_for(self._receive_task, None)
            except asyncio.CancelledError:
                pass

    def init_ddb(self, ddb):
        self.ddb = ddb

    def update_ddb(self, mod):
        channel_names = dict()
        for name, desc in self.ddb.items():
            if isinstance(desc, dict) and desc["type"] == "local":
                if "arguments" in desc and "channel" in desc["arguments"]:
                    channel = desc["arguments"]["channel"]
                    channel_names[channel] = name
                elif desc["type"] == "controller" and name == "core_analyzer":
                    self.rtio_addr = desc["host"]
                    self.rtio_port = desc.get("port_proxy", 1382)
        self.channel_mgr.channel_names = channel_names

    # handler for ccb
    def ccb_notify(self, message):
        try:
            service = message["service"]
            if service == "show_trace":
                asyncio.ensure_future(self._sync_proxy_task())
        except:
            logger.error("failed to process CCB", exc_info=True)

