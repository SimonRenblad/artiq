from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient
from sipyco import pyon

from artiq.tools import exc_to_warning
from artiq.gui.tools import LayoutWidget, get_open_file_name, get_save_file_name
from artiq.coredevice.comm_analyzer import decode_dump, decoded_dump_to_waveform, decoded_dump_to_vcd

import numpy as np
import pyqtgraph as pg
import asyncio
import time
import logging

logger = logging.getLogger(__name__)

class _AddChannelDialog(QtWidgets.QDialog):
    accepted = QtCore.pyqtSignal(list)

    def __init__(self, parent, channels):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.setWindowTitle("Add channels")   
        self.parent = parent

        grid = QtWidgets.QGridLayout()
        grid.setRowMinimumHeight(1, 40)
        grid.setColumnMinimumWidth(2, 60)
        self.setLayout(grid)

        self.waveform_channel_list = QtWidgets.QListWidget()
        self.waveform_channel_list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        grid.addWidget(self.waveform_channel_list, 0, 0, 1, 2)
        self.waveform_channel_list.itemDoubleClicked.connect(self.add_channels)
        for channel in sorted(channels):
            self.waveform_channel_list.addItem(channel)

        enter_action = QtWidgets.QAction("Add channels", self)
        enter_action.setShortcut("RETURN")
        enter_action.setShortcutContext(Qt.WidgetShortcut)
        self.addAction(enter_action)
        enter_action.triggered.connect(self.add_channels)

        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.clicked.connect(self.close)
        grid.addWidget(cancel_button, 1, 0)

        confirm_button = QtWidgets.QPushButton("Confirm")
        confirm_button.clicked.connect(self.add_channels)
        grid.addWidget(confirm_button, 1, 1)

    def add_channels(self):
        channels = self.waveform_channel_list.selectedItems()
        channels = [c.text() for c in channels]
        self.accepted.emit(channels)
        self.close()


class _ChannelWidget(QtWidgets.QWidget):

    def __init__(self, channel, parent=None):
        QtWidgets.QWidget.__init__(self, parent=parent)
        self.channel = channel
        self.parent = parent
        self.setMinimumHeight(300)

        frame_layout = QtWidgets.QVBoxLayout()
        frame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.Box)
        frame_layout.addWidget(frame)
        self.setLayout(frame_layout)
        layout = QtWidgets.QHBoxLayout()
        frame.setLayout(layout)
        self.label = QtWidgets.QLabel(channel)
        self.label.setMinimumWidth(50)
        layout.addWidget(self.label, 2)

        pi = pg.PlotItem(x=None,
                         y=None,
                         pen="r",
                         stepMode="right",
                         connect="finite")
        pi.showGrid(x=True, y=True)
        pi.getAxis("left").setStyle(tickTextWidth=100, autoExpandTextSpace=False)
        self.waveform = pg.PlotWidget(plotItem=pi)
        layout.addWidget(self.waveform, 8)

        self.label.setContextMenuPolicy(Qt.ActionsContextMenu)
        insert_action = QtWidgets.QAction("Insert channels below...", self)
        insert_action.triggered.connect(self.insert_channel)
        self.label.addAction(insert_action)
        move_up_action = QtWidgets.QAction("Move channel up", self)
        move_up_action.triggered.connect(self.move_channel_up)
        self.label.addAction(move_up_action)
        move_down_action = QtWidgets.QAction("Move channel down", self)
        move_down_action.triggered.connect(self.move_channel_down)
        self.label.addAction(move_down_action)
        remove_channel_action = QtWidgets.QAction("Delete channel", self)
        remove_channel_action.triggered.connect(self.remove_channel)
        self.label.addAction(remove_channel_action)

    def load_data(self, data):
        try:
            y_data, x_data = zip(*data)
            self.waveform.getPlotItem().listDataItems()[0].setData(x=x_data, y=y_data)
        except:
            logger.warn("Unable to load data for {}".format(self.channel), exc_info=1)
            self.waveform.getPlotItem().listDataItems()[0].setData(x=np.zeros(1), y=np.zeros(1))

    def insert_channel(self):
        next_ind = self.parent.plot_widgets.index(self) + 1
        self.parent.insert_plot_dialog(next_ind)

    def move_channel_up(self):
        ind = self.parent.plot_widgets.index(self)
        if ind != 0:
            self.parent.move_up(ind)

    def move_channel_down(self):
        ind = self.parent.plot_widgets.index(self)
        l = len(self.parent.plot_widgets)
        if ind != l - 1:
            self.parent.move_down(ind)

    def remove_channel(self):
        ind = self.parent.plot_widgets.index(self)
        self.parent.remove_plot(ind)


class _WaveformWidget(QtWidgets.QWidget):
    mouseMoved = QtCore.pyqtSignal(float, float)

    def __init__(self, parent, trace):
        QtWidgets.QWidget.__init__(self, parent=parent)
        self.trace = trace

        self.plot_layout = QtWidgets.QVBoxLayout()
        self.plot_layout.setSpacing(1) 
        scroll_area = QtWidgets.QScrollArea()
        scroll_area.setWidgetResizable(True)
        widget = QtWidgets.QWidget()
        widget.setLayout(self.plot_layout)
        scroll_area.setWidget(widget)
        main_layout = QtWidgets.QVBoxLayout()
        main_layout.addWidget(scroll_area)
        self.setLayout(main_layout)
        self.plot_widgets = list()

    async def get_channels_from_dialog(self):
        channels = self.trace["channels"]
        dialog = _AddChannelDialog(self, channels)
        fut = asyncio.Future()
        def on_accept(s):
            fut.set_result(s)
        dialog.accepted.connect(on_accept)
        dialog.open()
        return await fut

    def add_plot(self, channel):
        data = self.trace["data"]
        channel_widget = _ChannelWidget(channel, parent=self)
        if channel in data:
            channel_widget.load_data(data[channel])
        self.plot_layout.addWidget(channel_widget)
        self.plot_widgets.append(channel_widget)

    async def add_plots_dialog_task(self):
        channels = await self.get_channels_from_dialog()
        for channel in channels:
            self.add_plot(channel)

    def add_plots_dialog(self):
        asyncio.ensure_future(self.add_plots_dialog_task())

    def insert_plot(self, channel, index):
        data = self.trace["data"]
        channel_widget = _ChannelWidget(channel, parent=self)
        if channel in data:
            channel_widget.load_data(data[channel])
        self.plot_layout.insertWidget(index, channel_widget)
        self.plot_widgets.insert(index, channel_widget)

    async def insert_plot_dialog_task(self, index):
        channels = await self.get_channels_from_dialog()
        for channel in channels:
            self.insert_plot(channel, index)

    def insert_plot_dialog(self, index):
        asyncio.ensure_future(self.insert_plot_dialog_task(index))

    def remove_plot(self, index):
        widget = self.plot_layout.takeAt(index)
        self.plot_widgets.pop(index)
        widget.widget().deleteLater()

    def clear_plots(self):
        for i in reversed(range(len(self.plot_widgets))):
            self.remove_plot(i)

    def move_down(self, index):
        self.plot_layout.takeAt(index)
        widget = self.plot_widgets.pop(index)
        self.plot_layout.insertWidget(index+1, widget)
        self.plot_widgets.insert(index+1, widget)
    
    def move_up(self, index):
        self.plot_layout.takeAt(index)
        widget = self.plot_widgets.pop(index)
        self.plot_layout.insertWidget(index-1, widget)
        self.plot_widgets.insert(index-1, widget)

    def refresh_display(self):
        data = self.trace["data"]
        for widget in self.plot_widgets:
            channel = widget.channel
            widget.load_data(data[channel])

    def prepare_save_list(self):
        save_list = list()
        for widget in self.plot_widgets:
            save_list.append(widget.channel)
        return pyon.encode(save_list)

    def read_save_list(self, save_list):
        save_list = pyon.decode(save_list)
        for i in reversed(range(len(self.plot_widgets))):
            self.remove_plot(i)

        for channel in save_list:
            self.add_plot(channel)

    async def save_list_task(self):
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
            save_list = self.prepare_save_list()
            with open(filename, 'w') as f:
                f.write(save_list)
        except:
            logger.error("Failed to save channel list",
                         exc_info=True)

    async def open_list_task(self):
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
                self.read_save_list(f.read())
        except:
            logger.error("Failed to read channel list.",
                         exc_info=True)

class _TraceManager:
    def __init__(self, parent, trace, loop):
        self.parent = parent
        self.trace = trace
        self._loop = loop
        self.rtio_addr = None
        self.rtio_port = None
        self.rtio_port_control = None
        self.dump = None
        self.decoded_dump = None
        self.subscriber = Subscriber("devices", self.init_ddb, self.update_ddb)
        self.proxy_client = AsyncioClient()
        self.trace_subscriber = Subscriber("rtio_trace", self.init_dump, self.update_dump) 
        self.proxy_reconnect = asyncio.Event()
        self.dump_updated = asyncio.Event()
        self.reconnect_task = None

    def update_from_dump(self, dump):
        self.dump = dump
        self.decoded_dump = decode_dump(dump)
        decoded_dump_to_waveform(self.trace, self.ddb, self.decoded_dump)
        self.parent.traceDataChanged.emit()
        self.dump_updated.set()

    async def open_trace_task(self):
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
            self.update_from_dump(dump)
        except:
            logger.error("Failed to parse binary trace file",
                         exc_info=True)

    async def save_trace_task(self):
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
            
    async def pull_from_device_task(self):
        try:
            asyncio.ensure_future(exc_to_warning(self.proxy_client.pull_from_device()))
        except:
            logger.error("Pull from device failed, is proxy running?", exc_info=1)

    async def save_vcd_task(self):
        try:
            filename = await get_save_file_name(
                    self.parent,
                    "Save VCD",
                    "c://",
                    "Value Change Dump (*.vcd)")
        except asyncio.CancelledError:
            return
        try:
            with open(filename, 'w') as f:
                decoded_dump_to_vcd(f, self.ddb, self.decoded_dump)
        except:
            logger.error("Failed to save dump to VCD", exc_info=1)

    # Proxy subscriber callbacks
    def init_dump(self, dump):
        return dump

    def update_dump(self, mod):
        dump = mod.get("value", None)
        if dump:
            self.update_from_dump(dump)
   
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
                pass
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
        devices = self.ddb
        for name, desc in devices.items():
            if isinstance(desc, dict):
                if desc["type"] == "controller" and name == "core_analyzer":
                    self.rtio_addr = desc["host"]
                    self.rtio_port = desc.get("port_proxy", 1382)
                    self.rtio_port_control = desc.get("port_proxy_control", 1385)
        if self.rtio_addr is not None:
            self.proxy_reconnect.set()
    
    # Experiment and applet handling
    async def ccb_pull_trace(self, channels=None):
        try:
            await self.proxy_client.pull_from_device()
            await self.dump_updated.wait()
            self.dump_updated.clear()
            self.parent.clearActiveChannelsSignal.emit()
            for name in channels:
                self.parent.addActiveChannelSignal.emit(name)
        except:
            logger.error("Error pulling from proxy, is proxy connected?", exc_info=1)

    def ccb_notify(self, message):
        try:
            service = message["service"]
            args = message["args"]
            kwargs = message["kwargs"]
            if service == "pull_trace_from_device":
                asyncio.ensure_future(exc_to_warning(self.ccb_pull_trace(*args, **kwargs)))
        except:
            logger.error("failed to process CCB", exc_info=True)


class WaveformDock(QtWidgets.QDockWidget):
    traceDataChanged = QtCore.pyqtSignal()
    addActiveChannelSignal = QtCore.pyqtSignal(str)
    clearActiveChannelsSignal = QtCore.pyqtSignal()

    def __init__(self, loop=None):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(QtWidgets.QDockWidget.DockWidgetMovable |
                         QtWidgets.QDockWidget.DockWidgetFloatable)

        self.trace = {"channels": set(), "logs": set(), "data": dict()}
        self.tm = _TraceManager(self, self.trace, loop)

        grid = LayoutWidget()
        self.setWidget(grid)

        self.menu_button = QtWidgets.QPushButton()
        self.menu_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_FileDialogStart))
        grid.addWidget(self.menu_button, 0, 0)
        
        self.pull_button = QtWidgets.QToolButton()
        self.pull_button.setToolTip("Pull device buffer")
        self.pull_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_BrowserReload))
        grid.addWidget(self.pull_button, 0, 1)
        self.pull_button.clicked.connect(
                lambda: asyncio.ensure_future(self.tm.pull_from_device_task()))

        self.waveform_widget = _WaveformWidget(self, self.trace) 
        self.traceDataChanged.connect(self.waveform_widget.refresh_display)
        self.addActiveChannelSignal.connect(self.waveform_widget.add_plot)
        self.clearActiveChannelsSignal.connect(self.waveform_widget.clear_plots)
        grid.addWidget(self.waveform_widget, 2, 0, colspan=12)

        self.add_button = QtWidgets.QToolButton()
        self.add_button.setToolTip("Add channels...")
        self.add_button.setIcon(
                QtWidgets.QApplication.style().standardIcon(
                    QtWidgets.QStyle.SP_FileDialogListView))
        grid.addWidget(self.add_button, 0, 2)
        self.add_button.clicked.connect(self.waveform_widget.add_plots_dialog)
        
        self.traceDataChanged.connect(self.write_logs) 

        file_menu = QtWidgets.QMenu()

        open_trace_action = QtWidgets.QAction("Open trace...", self)
        open_trace_action.triggered.connect(
                lambda: asyncio.ensure_future(self.tm.open_trace_task()))
        file_menu.addAction(open_trace_action)

        save_trace_action = QtWidgets.QAction("Save trace...", self)
        save_trace_action.triggered.connect(
                lambda: asyncio.ensure_future(self.tm.save_trace_task()))
        file_menu.addAction(save_trace_action)

        open_list_action = QtWidgets.QAction("Open channel list...", self)
        open_list_action.triggered.connect(
                lambda: asyncio.ensure_future(self.waveform_widget.open_list_task()))
        file_menu.addAction(open_list_action)

        save_list_action = QtWidgets.QAction("Save channel list...", self)
        save_list_action.triggered.connect(
                lambda: asyncio.ensure_future(self.waveform_widget.save_list_task()))
        file_menu.addAction(save_list_action)

        save_vcd_action = QtWidgets.QAction("Save VCD...", self)
        save_vcd_action.triggered.connect(
                lambda: asyncio.ensure_future(self.tm.save_vcd_task()))
        file_menu.addAction(save_vcd_action)

        self.menu_button.setMenu(file_menu)

    @staticmethod
    def extract_logs(data):
        out_data = []
        for m in data:
            log = ""
            while m > 0:
                log += chr(m & 0xff)
                m >>= 8
            out_data.append(log[::-1])
        return out_data

    def write_logs(self):
        for log in sorted(self.trace["logs"]):
            data = self.trace["data"][log]
            msgs, times = zip(*data)
            msgs = self.extract_logs(msgs)
            for msg, time in zip(msgs, times):
                logger.info("{}@{}: {}".format(log, time, msg))
