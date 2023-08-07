from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.keepalive import async_open_connection
from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient
from sipyco import pyon

from artiq.tools import exc_to_warning
from artiq.gui.tools import LayoutWidget, get_open_file_name, get_save_file_name
from artiq.coredevice.comm_analyzer import decode_dump

from enum import Enum
from dataclasses import dataclass
import numpy as np
import pyqtgraph as pg
import collections
import math
import itertools
import asyncio
import struct
import time
import atexit
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


class _AddChannelDialog(QtWidgets.QDialog):

    def __init__(self, parent, channel_mgr=None):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
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

        enter_action = QtWidgets.QAction("Add channel", self)
        enter_action.setShortcut("RETURN")
        enter_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(enter_action)
        enter_action.triggered.connect(
                lambda: self.add_channel(self.waveform_channel_list.currentItem()))

    def add_channel(self, channel):
        self.parent.add_channel(channel.text())
        self.close()

    def update_channels(self):
        self.waveform_channel_list.clear()
        for channel in self.cmgr.channels:
            name = channel[0]
            self.waveform_channel_list.addItem(name)


class _ChannelDisplaySettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, channel_mgr=None, channel_ind=None):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setWindowTitle("Display Channel Settings")
        self.cmgr = channel_mgr
        self.channel_ind = channel_ind
        self.channel = self.cmgr.active_channels[channel_ind]

        grid = QtWidgets.QGridLayout()
        grid.setRowMinimumHeight(1, 40)
        grid.setColumnMinimumWidth(2, 60)
        self.setLayout(grid)
        
        msg_types = self.channel.visible_types
        
        self.output_visible = QtWidgets.QCheckBox("OutputMessage") 
        if msg_types[0]:
            self.output_visible.setChecked(True)
        grid.addWidget(self.output_visible, 0, 0)
        
        self.input_visible = QtWidgets.QCheckBox("InputMessage") 
        if msg_types[1]:
            self.input_visible.setChecked(True)
        grid.addWidget(self.input_visible, 1, 0)

        self.exception_visible = QtWidgets.QCheckBox("ExceptionMessage")
        if msg_types[2]:
            self.exception_visible.setChecked(True)
        grid.addWidget(self.exception_visible, 2, 0)

        display_types = self.channel.display_types
        all_display_types = [ty.name for ty in DisplayType]
       
        self.displaytype_out = QtWidgets.QComboBox()
        self.displaytype_out.addItems(all_display_types)
        self.displaytype_out.setCurrentIndex(display_types[0].value)
        grid.addWidget(self.displaytype_out, 0, 1)

        self.displaytype_in = QtWidgets.QComboBox()
        self.displaytype_in.addItems(all_display_types)
        self.displaytype_in.setCurrentIndex(display_types[1].value)
        grid.addWidget(self.displaytype_in, 1, 1)

        self.cancel = QtWidgets.QPushButton("Cancel")
        self.cancel.clicked.connect(self.close)
        grid.addWidget(self.cancel, 3, 0)

        self.confirm = QtWidgets.QPushButton("Confirm")
        self.confirm.clicked.connect(self._confirm_filter)
        grid.addWidget(self.confirm, 3, 1)
    
    def _confirm_filter(self):
        visible_types = (
                self.output_visible.isChecked(),
                self.input_visible.isChecked(),
                self.exception_visible.isChecked()
        )
        self.channel.visible_types = visible_types
        display_types = (
            DisplayType[self.displaytype_out.currentText()],
            DisplayType[self.displaytype_in.currentText()]
        )
        self.channel.display_types = display_types
        self.cmgr.active_channels[self.channel_ind] = self.channel
        self.cmgr.broadcast_active()
        self.close()


class _ActiveChannelList(QtWidgets.QListWidget):

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
        self.add_channel_dialog = _AddChannelDialog(self, channel_mgr=self.cmgr)
        add_channel_action = QtWidgets.QAction("Add channel...", self)
        add_channel_action.triggered.connect(lambda: self.add_channel_dialog.open())
        add_channel_action.setShortcut("CTRL+N")
        add_channel_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(add_channel_action)

        # Message type to display
        display_settings_action = QtWidgets.QAction("Display channel settings...", self)
        display_settings_action.triggered.connect(self._display_channel_settings)
        display_settings_action.setShortcut("RETURN")
        display_settings_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(display_settings_action)
        self.itemDoubleClicked.connect(lambda item: self._display_channel_settings())

        # Save list 
        save_list_action = QtWidgets.QAction("Save active list...", self)
        save_list_action.triggered.connect(lambda: asyncio.ensure_future(self._save_list_task()))
        save_list_action.setShortcut("CTRL+S")
        save_list_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(save_list_action)

        # Load list
        load_list_action = QtWidgets.QAction("Load active list...", self)
        load_list_action.triggered.connect(lambda: asyncio.ensure_future(self._load_list_task()))
        load_list_action.setShortcut("CTRL+L")
        load_list_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(load_list_action)

        # Remove channel
        remove_channel_action = QtWidgets.QAction("Delete channel", self)
        remove_channel_action.triggered.connect(self.remove_channel)
        remove_channel_action.setShortcut("DEL")
        remove_channel_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(remove_channel_action)

        self.cmgr.addActiveChannelSignal.connect(self.add_channel)

    def _prepare_save_list(self):
        save_list = list()
        for channel in self.cmgr.active_channels:
            channel_dict = channel.describe()
            save_list.append(channel)
        return pyon.encode(save_list)

    def _read_save_list(self, save_list):
        self.clear()
        self.cmgr.active_channels = list()
        save_list = pyon.decode(save_list)
        for channel in save_list:
            id = channel['id']
            name = channel['name']
            visible_types = channel['visible_types']
            display_types = channel['display_types']
            channel_obj = Channel(name, id, display_types, visible_types)
            self.cmgr.active_channels.append(channel_obj)
            self.addItem(name)
        self.cmgr.broadcast_active()

    async def _save_list_task(self):
        try:
            filename = await get_save_file_name(
                    self,
                    "Save Channel List",
                    "c://",
                    "PYON files (*.pyon)",
                    suffix="pyon")
        except asyncio.CancelledError:
            return
        try:
            save_list = self._prepare_save_list()
            with open(filename, 'w') as f:
                f.write(save_list)
        except:
            logger.error("Failed to save channel list",
                         exc_info=True)

    async def _load_list_task(self):
        try:
            filename = await get_open_file_name(
                    self,
                    "Load Channel List",
                    "c://",
                    "PYON files (*.pyon)")
        except asyncio.CancelledError:
            return
        try:
            with open(filename, 'r') as f:
                self._read_save_list(f.read())
        except:
            logger.error("Failed to read channel list.",
                         exc_info=True)

    def _display_channel_settings(self):
        item = self.currentItem()
        ind = self.row(item)
        dialog = _ChannelDisplaySettingsDialog(self, 
                                         channel_mgr=self.cmgr,
                                         channel_ind=ind)
        dialog.open()

    def remove_channel(self):
        item = self.currentItem()
        ind = self.row(item)
        self.takeItem(ind)
        self.cmgr.active_channels.pop(ind)
        self.cmgr.broadcast_active()

    def add_channel(self, name):
        channel_id = self.cmgr.get_channel_id(name)
        channel = Channel(name, channel_id)
        self.addItem(name)
        self.cmgr.active_channels.append(channel)
        self.cmgr.broadcast_active()


class _WaveformWidget(pg.PlotWidget):
    mouseMoved = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None, channel_mgr=None):
        pg.PlotWidget.__init__(self, parent=parent)
        self.addLegend()
        self.showGrid(True, True, 0.5)
        self.setLabel('bottom', text='time', units='s') # TODO: necessary?
        self.cmgr = channel_mgr
        self.cmgr.activeChannelsChanged.connect(self.refresh_display)
        self.cmgr.traceDataChanged.connect(self.refresh_display)
        self._plots = list()
        self.refresh_display()
        self.proxy = pg.SignalProxy(self.scene().sigMouseMoved, rateLimit=60, slot=self.get_cursor_coordinates)

    def get_cursor_coordinates(self, event):
        mousePoint = self.getPlotItem().vb.mapSceneToView(event[0])
        self.mouseMoved.emit(mousePoint.x(), mousePoint.y())
    
    def refresh_display(self):
        start = time.monotonic()
        self._display_graph()
        end = time.monotonic()
        logger.info(f"Refresh took {(end - start)*1000} ms")

    def _display_graph(self):
        for plot in self._plots:
            self.removeItem(plot)
        self._plots = list()

        for channel in self.cmgr.active_channels:
            for i in range(3):
                if channel.visible_types[i]:
                    self._display_waveform(channel, MessageType(i))
    
    @staticmethod
    def _convert_type(data, display_type):
        if display_type == DisplayType.INT_64:
            return data
        if display_type == DisplayType.FLOAT_64:
            return struct.unpack('>d', struct.pack('>Q', data))[0]

    def _display_waveform(self, channel, msg_type):
        data = self.cmgr.data.get(channel.id, {}).get(msg_type.value, [])
        if len(data) == 0:
            return
        x_data = np.zeros(len(data))
        y_data = np.zeros(len(data))
        for i, x in enumerate(data):
            x_data[i] = x.rtio_counter

        pen = None
        symbol = None
        if msg_type in [MessageType.OutputMessage, MessageType.InputMessage]:
            display_type = channel.display_types[msg_type.value]
            for i, y in enumerate(data):
                y_data[i] = self._convert_type(y.data, display_type)
            pen = {'color': msg_type.value, 'width': 1}
        else: # ExceptionMessage
            symbol = 'x'

        pdi = self.plot(x_data,
                        y_data,
                        symbol=symbol,
                        name=f"Channel: {channel.name}, {msg_type.name}",
                        pen=pen)
        self._plots.append(pdi)
        return

@dataclass
class Channel:
    name: str
    id: int
    display_types: tuple = (DisplayType.INT_64, DisplayType.INT_64)
    visible_types: tuple = (True, True, True)


class _ChannelManager(QtCore.QObject):
    activeChannelsChanged = QtCore.pyqtSignal()
    traceDataChanged = QtCore.pyqtSignal()
    addActiveChannelSignal = QtCore.pyqtSignal(str)

    def __init__(self):
        QtCore.QObject.__init__(self) 
        self.data = dict()
        self.active_channels = list()
        self.channels = list()

    def set_channel_active(self, name):
        selected = None
        for channel in self.channels:
            if channel[0] == name:
                selected = channel
                break
        new_active_channel = Channel(name, id)
        self.active_channels.append(new_active_channel)

    def get_channel_name(self, id):
        for channel in self.channels:
            if channel[1] == id:
                return channel[0]

    def get_channel_id(self, name):
        for channel in self.channels:
            if channel[0] == name:
                return channel[1]

    def broadcast_active(self):
        self.activeChannelsChanged.emit()

    def broadcast_data(self):
        self.traceDataChanged.emit()


class _TraceManager:
    def __init__(self, parent, channel_mgr, loop):
        self.parent = parent
        self.cmgr = channel_mgr
        self._loop = loop
        self.rtio_addr = None
        self.rtio_port = None
        self.rtio_port_control = None
        self.dump = None
        self.subscriber = Subscriber("devices", self.init_ddb, self.update_ddb)
        self.proxy_client = AsyncioClient()
        self.trace_subscriber = Subscriber("rtio_trace", self.init_dump, self.update_dump) 
        self.proxy_reconnect = asyncio.Event()
        self.dump_updated = asyncio.Event()
        self.reconnect_task = None

    # parsing and loading dump
    @staticmethod
    def _parse_messages(messages):
        channels = list()
        data = dict()
        for message in messages:
            # get class name directly to avoid name conflict with comm_analyzer.MessageType
            msg_type = MessageType[message.__class__.__name__]
            if msg_type == MessageType.StoppedMessage:
                break
            channel_id = message.channel
            data.setdefault(channel_id, {})
            data[channel_id].setdefault(msg_type.value, [])
            data[channel_id][msg_type.value].append(message)
        return data

    def _update_from_dump(self, dump):
        self.dump = dump
        decoded_dump = decode_dump(dump)
        messages = decoded_dump.messages

        data = self._parse_messages(messages)

        self.cmgr.data = data
        self.cmgr.traceDataChanged.emit()
        self.dump_updated.set()

    async def _load_trace_task(self):
        try:
            filename = await get_open_file_name(
                    self.parent,
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
                    self.parent,
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
            
    async def _pull_from_device_task(self):
        try:
            asyncio.ensure_future(exc_to_warning(self.proxy_client.pull_from_device()))
        except:
            logger.error("Pull from device failed, is proxy running?", exc_info=1)

    # Proxy subscriber callbacks
    def init_dump(self, dump):
        return dump

    def update_dump(self, mod):
        dump = mod.get("value", None)
        if dump:
            self._update_from_dump(dump)
   
    # Proxy client connections
    async def start(self, server, port):
        # non-blocking, with loop to attach Subscriber and AsyncioClient
        self.reconnect_task = asyncio.ensure_future(self.reconnect(), loop = self._loop)
        try:
            await self.subscriber.connect(server, port)
        except:
            logger.error("Failed to connect to master.", exc_info=1)

    async def reconnect(self):
        while True:
            await self.proxy_reconnect.wait()
            self.proxy_reconnect.clear()
            try:
                self.proxy_client.close_rpc()
                await self.trace_subscriber.close()
            except:
                pass # will throw if not connected
            try:
                await self.proxy_client.connect_rpc(self.rtio_addr, self.rtio_port_control, "rtio_proxy_control")
                await self.trace_subscriber.connect(self.rtio_addr, self.rtio_port)
            except TimeoutError:
                await asyncio.sleep(5)
                self.proxy_reconnect.set()
            except:
                logger.error("Proxy reconnect failed, is proxy running?")
            else:
                logger.info(f"Proxy connected on host {self.rtio_addr}")

    async def stop(self):
        self.reconnect_task.cancel()
        try:
            await asyncio.wait_for(self.reconnect_task, None)
        except asyncio.CancelledError:
            pass
        try:
            await self.subscriber.close()
            self.proxy_client.close_rpc()
            await self.trace_subscriber.close()
        except:
            logger.error("Error closing proxy connections")
    
    # DeviceDB subscriber callbacks
    def init_ddb(self, ddb):
        self.ddb = ddb

    def update_ddb(self, mod):
        channels = list()
        for name, desc in self.ddb.items():
            if isinstance(desc, dict):
                if "arguments" in desc and "channel" in desc["arguments"] and desc["type"] == "local":
                    channel = desc["arguments"]["channel"]
                    channels.append((name, channel))
                elif desc["type"] == "controller" and name == "core_analyzer":
                    self.rtio_addr = desc["host"]
                    self.rtio_port = desc.get("port_proxy", 1382)
                    self.rtio_port_control = desc.get("port_proxy_control", 1385)
        self.cmgr.channels = channels
        self.cmgr.traceDataChanged.emit() # change to other signal
        if self.rtio_addr is not None:
            self.proxy_reconnect.set()
    
    # Experiment and applet handling
    async def ccb_pull_helper(self, channels=None):
        try:
            await self.proxy_client.pull_from_device()
            await self.dump_updated.wait()
            self.dump_updated.clear()
            self.cmgr.active_channels.clear()
            for name in channels:
                self.cmgr.addActiveChannelSignal.emit(name)
        except:
            logger.error("Error pulling from proxy, is proxy connected?", exc_info=1)

    def ccb_notify(self, message):
        try:
            service = message["service"]
            args = message["args"]
            kwargs = message["kwargs"]
            if service == "pull_trace_from_device":
                task = asyncio.ensure_future(exc_to_warning(self.ccb_pull_helper(**kwargs)))
        except:
            logger.error("failed to process CCB", exc_info=True)

class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self, loop=None):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)

        self.cmgr = _ChannelManager()
        self.tm = _TraceManager(parent=self, channel_mgr=self.cmgr, loop=loop)

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

        self.pull_button = QtWidgets.QPushButton("Pull from Device Buffer")
        self.pull_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_ArrowUp))
        grid.addWidget(self.pull_button, 0, 2)
        self.pull_button.clicked.connect(self._pull_from_device_clicked)

        self.x_coord_label = QtWidgets.QLabel("x:")
        self.x_coord_label.setFont(QtGui.QFont("Monospace", 10))
        grid.addWidget(self.x_coord_label, 1, 2, colspan=1)

        self.y_coord_label = QtWidgets.QLabel("y:")
        self.y_coord_label.setFont(QtGui.QFont("Monospace", 10))
        grid.addWidget(self.y_coord_label, 1, 3, colspan=9)
        
        self.waveform_active_channel_view = _ActiveChannelList(channel_mgr=self.cmgr)
        grid.addWidget(self.waveform_active_channel_view, 2, 0, colspan=2)
        self.waveform_widget = _WaveformWidget(channel_mgr=self.cmgr) 
        grid.addWidget(self.waveform_widget, 2, 2, colspan=10)
        self.waveform_widget.mouseMoved.connect(self.update_coord_label)

    def update_coord_label(self, coord_x, coord_y):
        self.x_coord_label.setText(f"x: {coord_x:.10g}")
        self.y_coord_label.setText(f"y: {coord_y:.10g}")

    def _load_trace_clicked(self):
        asyncio.ensure_future(self.tm._load_trace_task())

    def _save_trace_clicked(self):
        asyncio.ensure_future(self.tm._save_trace_task())
    
    def _pull_from_device_clicked(self):
        asyncio.ensure_future(self.tm._pull_from_device_task())
