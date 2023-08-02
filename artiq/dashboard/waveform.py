from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.keepalive import async_open_connection
from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient
from sipyco import pyon
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
        enter_action = QtWidgets.QAction("Add channel", self)
        enter_action.setShortcut("RETURN")
        enter_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(enter_action)
        enter_action.triggered.connect(
                lambda: self.add_channel(self.waveform_channel_list.currentItem()))
        self.cmgr.traceDataChanged.connect(self.update_channels)

    def add_channel(self, channel):
        self.parent.add_channel(channel.text())
        self.close()

    def update_channels(self):
        self.waveform_channel_list.clear()
        for channel in self.cmgr.channels:
            name = self.cmgr.channel_name_id_map.get_by_right(channel)
            self.waveform_channel_list.addItem(name)


class _ChannelDisplaySettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, channel_mgr=None, channel=None):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setWindowTitle("Display Channel Settings")
        self.cmgr = channel_mgr
        self.channel = channel

        grid = QtWidgets.QGridLayout()
        grid.setRowMinimumHeight(1, 40)
        grid.setColumnMinimumWidth(2, 60)
        self.setLayout(grid)

        msg_types = self.cmgr.msg_types[self.channel]
        
        self.output_visible = QtWidgets.QCheckBox("OutputMessage") 
        if MessageType.OutputMessage in msg_types:
            self.output_visible.setChecked(True)
        grid.addWidget(self.output_visible, 0, 0)
        
        self.input_visible = QtWidgets.QCheckBox("InputMessage") 
        if MessageType.InputMessage in msg_types:
            self.input_visible.setChecked(True)
        grid.addWidget(self.input_visible, 1, 0)

        self.exception_visible = QtWidgets.QCheckBox("ExceptionMessage")
        if MessageType.ExceptionMessage in msg_types:
            self.exception_visible.setChecked(True)
        grid.addWidget(self.exception_visible, 2, 0)

        display_types = self.cmgr.display_types[self.channel]
       
        self.displaytype_out = QtWidgets.QComboBox()
        self.displaytype_out.addItems(["INT_64", "FLOAT_64"])
        self.displaytype_out.setCurrentIndex(display_types[0].value)
        grid.addWidget(self.displaytype_out, 0, 1)

        self.displaytype_in = QtWidgets.QComboBox()
        self.displaytype_in.addItems(["INT_64", "FLOAT_64"])
        self.displaytype_in.setCurrentIndex(display_types[1].value)
        grid.addWidget(self.displaytype_in, 1, 1)

        self.cancel = QtWidgets.QPushButton("Cancel")
        self.cancel.clicked.connect(self.close)
        grid.addWidget(self.cancel, 3, 0)

        self.confirm = QtWidgets.QPushButton("Confirm")
        self.confirm.clicked.connect(self._confirm_filter)
        grid.addWidget(self.confirm, 3, 1)
    
    def _confirm_filter(self):
        self.cmgr.msg_types[self.channel] = set()
        if self.output_visible.isChecked():
            self.cmgr.msg_types[self.channel].add(MessageType.OutputMessage)
        if self.input_visible.isChecked():
            self.cmgr.msg_types[self.channel].add(MessageType.InputMessage)
        if self.exception_visible.isChecked():
            self.cmgr.msg_types[self.channel].add(MessageType.ExceptionMessage)

        self.cmgr.display_types[self.channel][0] = DisplayType[self.displaytype_out.currentText()]
        self.cmgr.display_types[self.channel][1] = DisplayType[self.displaytype_in.currentText()]

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

        self.cmgr.traceDataChanged.connect(self.clear)

    def _prepare_save_list(self):
        save_list = list()
        for channel_id in self.cmgr.active_channels:
            channel = dict()
            channel['id'] = channel_id
            channel['name'] = self.cmgr.channel_name_id_map.get_by_right(channel_id)
            channel['msg_types'] = [x.value for x in self.cmgr.msg_types[channel_id]]
            channel['display_types'] = [x.value for x in self.cmgr.display_types[channel_id]]
            save_list.append(channel)
        return pyon.encode(save_list)

    def _read_save_list(self, save_list):
        self.clear()
        self.cmgr.active_channels = list()
        save_list = pyon.decode(save_list)
        for channel in save_list:
            id = channel['id']
            name = channel['name']
            self.cmgr.channel_name_id_map.add(name, id)
            self.cmgr.msg_types[id] = set([MessageType(x) for x in channel['msg_types']])
            self.cmgr.display_types[id] = [DisplayType(x) for x in channel['display_types']]
            self.addItem(name)
            self.cmgr.active_channels.append(id)
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

    # TODO: catch errors here
    def _selected_channel(self):
        item = self.currentItem()
        s = item.text()
        c = self.cmgr.channel_name_id_map.get_by_left(s)
        return item, c

    def _display_channel_settings(self):
        try:
            item, channel = self._selected_channel()
        except:
            return
        dialog = _ChannelDisplaySettingsDialog(self, 
                                         channel_mgr=self.cmgr,
                                         channel=channel)
        dialog.open()

    def remove_channel(self):
        try:
            item, channel = self._selected_channel()
        except:
            return
        self.takeItem(self.row(item))
        self.cmgr.active_channels.remove(channel)
        self.cmgr.broadcast_active()

    def add_channel(self, channel):
        channel_id = self.cmgr.channel_name_id_map.get_by_left(channel)
        if channel_id not in self.cmgr.active_channels:
            self.addItem(channel)
            self.cmgr.active_channels.append(channel_id)
        self.cmgr.broadcast_active()


class _WaveformWidget(pg.PlotWidget):
    mouseMoved = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None, channel_mgr=None):
        pg.PlotWidget.__init__(self, parent=parent)
        self.addLegend()
        self.showGrid(True, True, 0.5)
        self.setLabel('bottom', text='time', units='s')
        self.cmgr = channel_mgr
        self.cmgr.activeChannelsChanged.connect(self.refresh_display)
        self.cmgr.traceDataChanged.connect(self.refresh_display)
        self._plots = list()
        self.left_mouse_pressed = False
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
            for msg_type in self.cmgr.msg_types[channel]:
                self._display_waveform(channel, msg_type)
    
    @staticmethod
    def _convert_type(data, display_type):
        if display_type == DisplayType.INT_64:
            return data
        if display_type == DisplayType.FLOAT_64:
            return struct.unpack('>d', struct.pack('>Q', data))[0]

    def _display_waveform(self, channel, msg_type):
        data = self.cmgr.data[channel].get(msg_type, [])
        if len(data) == 0:
            return
        x_data = np.zeros(len(data))
        y_data = np.zeros(len(data))
        for i, x in enumerate(data):
            x_data[i] = x.rtio_counter

        pen = None
        symbol = None
        if msg_type in [MessageType.OutputMessage, MessageType.InputMessage]:
            display_type = self.cmgr.display_types[channel][msg_type.value]
            for i, y in enumerate(data):
                y_data[i] = self._convert_type(y.data, display_type)
            pen = {'color': msg_type.value, 'width': 1}
        else:
            symbol = 'x'

        pdi = self.plot(x_data,
                        y_data,
                        symbol=symbol,
                        name=f"Channel: {self.cmgr.channel_name_id_map.get_by_right(channel)}, {msg_type.name}",
                        pen=pen)
        self._plots.append(pdi)
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
        self.unit = 'ps'
        self.timescale = 1
        self.timescale_magnitude = 1

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
        self.reconnect_task = None

    # parsing and loading dump
    @staticmethod
    def _parse_messages(messages):
        channels = set()
        data = dict()
        msg_types = dict()
        display_types= dict()
        for message in messages:
            # get class name directly to avoid name conflict with comm_analyzer.MessageType
            message_type = MessageType[message.__class__.__name__]
            if message_type == MessageType.StoppedMessage:
                break

            c = message.channel
            v = message_type

            msg_types.setdefault(c, set())
            data.setdefault(c, {})
            data[c].setdefault(v, [])

            channels.add(c)
            msg_types[c].add(v)
            display_types[c] = [DisplayType.INT_64, DisplayType.INT_64]
            data[c][v].append(message)
        return channels, data, msg_types, display_types

    def _update_from_dump(self, dump):
        self.dump = dump
        decoded_dump = decode_dump(dump)
        messages = decoded_dump.messages

        channels, data, msg_types, display_types = self._parse_messages(messages)

        # default names if not defined in devicedb
        for c in channels:
            if c not in self.cmgr.channel_name_id_map.rights():
                self.cmgr.channel_name_id_map.add("unnamed_channel"+str(c), c)

        self.cmgr.channels = channels
        self.cmgr.data = data
        self.cmgr.msg_types = msg_types
        self.cmgr.display_types = display_types
        self.cmgr.active_channels = list()
        self.cmgr.traceDataChanged.emit()

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
                logger.error("Proxy reconnect failed, is proxy running?", exc_info=1)
            finally:
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
            logger.error("Error closing proxy connections", exc_info=1)
    
    # DeviceDB subscriber callbacks
    def init_ddb(self, ddb):
        self.ddb = ddb

    def update_ddb(self, mod):
        channel_name_id_map = BijectiveMap()
        for name, desc in self.ddb.items():
            if isinstance(desc, dict):
                if "arguments" in desc and "channel" in desc["arguments"] and desc["type"] == "local":
                    channel = desc["arguments"]["channel"]
                    channel_name_id_map.add(name, channel)
                elif desc["type"] == "controller" and name == "core_analyzer":
                    self.rtio_addr = desc["host"]
                    self.rtio_port = desc.get("port_proxy", 1382)
                    self.rtio_port_control = desc.get("port_proxy_control", 1385)
        self.cmgr.channel_name_id_map = channel_name_id_map
        if self.rtio_addr is not None:
            self.proxy_reconnect.set()

    # Experiment and applet handler
    def ccb_notify(self, message):
        try:
            service = message["service"]
            if service == "pull_trace_from_device":
                asyncio.ensure_future(exc_to_warning(self.proxy_client.pull_from_device()))
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


