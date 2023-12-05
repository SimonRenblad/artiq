from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient

from artiq.tools import exc_to_warning
from artiq.gui.tools import LayoutWidget, get_open_file_name, get_save_file_name
from artiq.gui.dndwidgets import DragDropSplitter, VDragScrollArea
from artiq.coredevice.comm_analyzer import (async_decoded_dump_to_vcd, get_channel_list,
                                            async_decoded_dump_to_waveform,
                                            async_decode_dump)
import os
import numpy as np
from operator import setitem
from enum import Enum
import pyqtgraph as pg
import asyncio
import logging

logger = logging.getLogger(__name__)


class _AddChannelDialog(QtWidgets.QDialog):
    accepted = QtCore.pyqtSignal(list)

    def __init__(self, parent, state):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.channels = state["channels"]

        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.setWindowTitle("Add channels")
        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)
        self._channels_widget = QtWidgets.QTreeWidget()
        self._channels_widget.setHeaderLabel("Channels")
        self._channels_widget.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        grid.addWidget(self._channels_widget, 0, 0, 1, 2)

        groups = dict()
        for scope, channel, _, _ in self.channels:
            if scope not in groups:
                group = QtWidgets.QTreeWidgetItem([scope])
                group.setFlags(group.flags() & ~QtCore.Qt.ItemIsSelectable)
                self._channels_widget.addTopLevelItem(group)
                groups[scope] = group
            item = QtWidgets.QTreeWidgetItem([channel])
            groups[scope].addChild(item)

        cancel_button = QtWidgets.QPushButton("Cancel")
        cancel_button.clicked.connect(self.close)
        cancel_button.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_DialogCancelButton))
        grid.addWidget(cancel_button, 1, 0)
        confirm_button = QtWidgets.QPushButton("Confirm")
        confirm_button.clicked.connect(self.add_channels)
        confirm_button.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_DialogApplyButton))
        grid.addWidget(confirm_button, 1, 1)

    def add_channels(self):
        items = self._channels_widget.selectedItems()
        items = [(i.parent().text(0), i.text(0)) for i in items]
        selected_channels = [channel for channel in self.channels
                             if channel[0:2] in items]
        self.accepted.emit(selected_channels)
        self.close()


DataFormat = Enum("DataFormat", ["INT", "HEX", "BIN", "REAL"])


def get_format_waveform_value(val, bit_width, data_format):
    lbl = str(val)
    hex_width = (bit_width - 1) // 4 + 1
    if val is not None:
        v = int(val)
        if data_format == DataFormat.INT:
            lbl = "{:d}".format(v)
        if data_format == DataFormat.HEX:
            lbl = "{v:0{w}X}".format(v=v, w=hex_width)
        elif data_format == DataFormat.BIN:
            lbl = "{v:0{w}b}".format(v=v, w=bit_width)
    else:
        if data_format == DataFormat.HEX:
            lbl = "X" * hex_width
        elif data_format == DataFormat.BIN:
            lbl = "x" * bit_width
        else:
            lbl = "nan"
    return lbl


class Waveform(pg.PlotWidget):
    MIN_HEIGHT = 100
    MAX_HEIGHT = 200
    PREF_HEIGHT = 150

    cursorMoved = QtCore.pyqtSignal(float)

    def __init__(self, channel, state, parent=None):
        pg.PlotWidget.__init__(self, parent=parent, x=None, y=None)
        self.setMinimumHeight(Waveform.MIN_HEIGHT)
        self.setMaximumHeight(Waveform.MAX_HEIGHT)
        self.setMenuEnabled(False)

        self._scope = channel[0]
        self._name = channel[1]
        self._ddb_name = channel[2]
        self._width = channel[3]

        self._state = state
        self._logs = list()
        self._symbol = "t"
        self._is_show_logs = True
        self._is_show_markers = False
        self._is_show_cursor = True
        self._is_digital = True

        self._pi = self.getPlotItem()
        self._pi.setRange(yRange=(0, 1), padding=0.1)
        self._pi.hideButtons()
        self._pi.getAxis("bottom").setStyle(showValues=False, tickLength=0)
        self._pi.hideAxis("top")

        self._pdi = self._pi.listDataItems()[0]
        pdi_opts = {
            "pen": "r",
            "stepMode": "right",
            "connect": "finite",
            "clipToView": True,
            "downsample": 10,
            "autoDownsample": True,
            "downsampleMethod": "peak",
            "symbolPen": "r",
            "symbolBrush": "r"
        }
        self._pdi.opts.update(pdi_opts)

        self._vb = self._pi.getViewBox()
        self._vb.setMouseEnabled(x=True, y=False)
        self._vb.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)

        self._left_ax = self._pi.getAxis("left")
        self._left_ax.setLabel(self._name)
        self._left_ax.label.setToolTip(self._name)
        self._left_ax.enableAutoSIPrefix(enable=False)
        self._left_ax.setWidth(120)

        self._cursor = pg.InfiniteLine()
        self._cursor_label = pg.InfLineLabel(self._cursor, text='0')
        self._cursor_y = None
        self.addItem(self._cursor)

        self._data_format = DataFormat.REAL

        self.setContextMenuPolicy(QtCore.Qt.ActionsContextMenu)
        self._set_digital(True)

    def _set_digital(self, digital):
        if digital:
            self._left_ax.setTicks([[(0, "0"), (1, "1")], []])
        else:
            self._left_ax.setTicks(None)
        self._is_digital = digital

    def _set_data_range(self, data_range):
        if data_range in [(0, 1), (0, 0)]:
            self._set_digital(True)
            self._pi.setRange(yRange=(0, 1), padding=0.1)
        else:
            self._set_digital(False)
            self._pi.setRange(yRange=data_range, padding=0.1)

    def on_load_data(self):
        data = self._state["data"]
        try:
            d = np.array(data[self._name])
            data_range = (np.min(d[:, 1]), np.max(d[:, 1]))
            self._set_data_range(data_range)
            self._pdi.setData(d)
        except:
            logger.debug("Unable to load data for {}/{}".format(self._scope, self._name))
            self._set_data_range((0, 0))
            self._pdi.setData(x=np.zeros(1), y=np.zeros(1))

    def on_load_logs(self):
        logs = self._state["logs"]
        msgs = logs.get(self._ddb_name, [])
        for t, msg in msgs:
            lbl = pg.TextItem(anchor=(0, 1))
            arw = pg.ArrowItem(angle=270, pxMode=True, headLen=5, tailLen=15, tailWidth=1)
            self.addItem(lbl)
            self.addItem(arw)
            lbl.setPos(t, 0)
            lbl.setText(msg)
            arw.setPos(t, 0)
            self._logs.append(lbl)
            self._logs.append(arw)

    def on_toggle_logs(self):
        if self._is_show_logs:
            for lbl in self._logs:
                self.removeItem(lbl)
            self._is_show_logs = False
        else:
            for lbl in self._logs:
                self.addItem(lbl)
            self._is_show_logs = True

    def on_set_cursor_visible(self, visible):
        if visible:
            self.removeItem(self._cursor)
            self._is_show_cursor = False
        else:
            self.addItem(self._cursor)
            self._is_show_cursor = True

    def on_toggle_markers(self):
        if self._is_show_markers:
            self._pdi.setSymbol(None)
            self._is_show_markers = False
        else:
            self._pdi.setSymbol(self._symbol)
            self._is_show_markers = True

    def _refresh_cursor_label(self):
        lbl = get_format_waveform_value(self._cursor_y, self._width, self._data_format)
        self._cursor_label.setText(lbl)

    def on_cursor_moved(self, x):
        self._cursor.setValue(x)
        ind = np.searchsorted(self._pdi.xData, x, side="left") - 1
        dr = self._pdi.dataRect()
        if dr is not None and dr.left() <= x <= dr.right() \
                and 0 <= ind < len(self._pdi.yData):
            self._cursor_y = self._pdi.yData[ind]
        else:
            self._cursor_y = None
        self._refresh_cursor_label()

    def on_set_int(self):
        self._data_format = DataFormat.INT
        self._refresh_cursor_label()

    def on_set_real(self):
        self._data_format = DataFormat.REAL
        self._refresh_cursor_label()

    def on_set_hex(self):
        self._data_format = DataFormat.HEX
        self._refresh_cursor_label()

    def on_set_bin(self):
        self._data_format = DataFormat.BIN
        self._refresh_cursor_label()

    # override
    def mouseMoveEvent(self, e):
        if e.buttons() == QtCore.Qt.LeftButton \
           and e.modifiers() != QtCore.Qt.NoModifier:
            drag = QtGui.QDrag(self)
            mime = QtCore.QMimeData()
            drag.setMimeData(mime)
            pixmapi = QtWidgets.QApplication.style().standardIcon(QtWidgets.QStyle.SP_FileIcon)
            drag.setPixmap(pixmapi.pixmap(32))
            drag.exec_(QtCore.Qt.MoveAction)
        else:
            super().mouseMoveEvent(e)

    # override
    def mouseDoubleClickEvent(self, e):
        pos = self._vb.mapSceneToView(e.pos())
        self.cursorMoved.emit(pos.x())


class WaveformArea(QtWidgets.QWidget):
    cursorMoved = QtCore.pyqtSignal(float)

    def __init__(self, parent, state):
        QtWidgets.QWidget.__init__(self, parent=parent)
        self._state = state

        self._is_show_cursor = True
        self._cursor_x_pos = 0

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)

        self._ref_axis = pg.PlotWidget()
        self._ref_axis.hideAxis("bottom")
        self._ref_axis.hideButtons()
        self._ref_axis.setFixedHeight(45)
        self._ref_axis.setMenuEnabled(False)
        top = pg.AxisItem("top")
        top.setLabel("", units="s")
        left = pg.AxisItem("left")
        left.setStyle(textFillLimits=(0, 0))
        left.setFixedHeight(0)
        left.setWidth(120)
        self._ref_axis.setAxisItems({"top": top, "left": left})

        self._ref_vb = self._ref_axis.getPlotItem().getViewBox()
        self._ref_vb.setFixedHeight(0)
        self._ref_vb.setMouseEnabled(x=True, y=False)
        layout.addWidget(self._ref_axis)

        scroll_area = VDragScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setContentsMargins(0, 0, 0, 0)
        scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)

        self._waveform_area = DragDropSplitter(parent=scroll_area)
        self._waveform_area.setHandleWidth(1)
        scroll_area.setWidget(self._waveform_area)

        layout.addWidget(scroll_area)

    def _add_waveform_actions(self, waveform):
        action = QtWidgets.QAction("Show RTIO logs", waveform)
        action.setCheckable(True)
        action.setChecked(True)
        action.triggered.connect(waveform.on_toggle_logs)
        waveform.addAction(action)

        action = QtWidgets.QAction("Show message markers", waveform)
        action.setCheckable(True)
        action.setChecked(False)
        action.triggered.connect(waveform.on_toggle_markers)
        waveform.addAction(action)

        action = QtWidgets.QAction("Data Format", waveform)
        menu = QtWidgets.QMenu(waveform)
        a1 = QtWidgets.QAction("Int", menu)
        a1.triggered.connect(waveform.on_set_int)
        a2 = QtWidgets.QAction("Real", menu)
        a2.triggered.connect(waveform.on_set_real)
        a3 = QtWidgets.QAction("Hex", menu)
        a3.triggered.connect(waveform.on_set_hex)
        a4 = QtWidgets.QAction("Bin", menu)
        a4.triggered.connect(waveform.on_set_bin)
        menu.addAction(a1)
        menu.addAction(a2)
        menu.addAction(a3)
        menu.addAction(a4)
        action.setMenu(menu)
        waveform.addAction(action)

        action = QtWidgets.QAction(waveform)
        action.setSeparator(True)
        waveform.addAction(action)

        action = QtWidgets.QAction("Toggle cursor visible", waveform)
        action.triggered.connect(self._on_toggle_cursor)
        waveform.addAction(action)

        action = QtWidgets.QAction("Delete waveform", waveform)
        action.triggered.connect(lambda: self._remove_plot(waveform))
        waveform.addAction(action)

        action = QtWidgets.QAction("Delete all", waveform)
        action.triggered.connect(self._clear_plots)
        waveform.addAction(action)

        action = QtWidgets.QAction("Reset waveform heights", waveform)
        action.triggered.connect(self._waveform_area.resetSizes)
        waveform.addAction(action)

    def _add_plot(self, channel):
        num_channels = self._waveform_area.count()
        self._waveform_area.setFixedHeight((num_channels + 1) * Waveform.PREF_HEIGHT)
        cw = Waveform(channel, self._state, parent=self._waveform_area)
        cw.cursorMoved.connect(lambda x: self.on_cursor_moved(x))
        cw.cursorMoved.connect(self.cursorMoved.emit)
        self._add_waveform_actions(cw)
        cw.setXLink(self._ref_vb)
        cw.getPlotItem().showGrid(x=True, y=True)
        self._waveform_area.addWidget(cw)
        cw.on_load_data()
        cw.on_load_logs()
        cw.on_cursor_moved(self._cursor_x_pos)

    async def _get_channels_from_dialog(self):
        dialog = _AddChannelDialog(self, self._state)
        fut = asyncio.Future()

        def on_accept(s):
            fut.set_result(s)
        dialog.accepted.connect(on_accept)
        dialog.open()
        return await fut

    async def _add_plots_dialog_task(self):
        channels = await self._get_channels_from_dialog()
        for channel in channels:
            self._add_plot(channel)

    def add_plots_dialog(self):
        asyncio.ensure_future(exc_to_warning(self._add_plots_dialog_task()))

    def _remove_plot(self, cw):
        num_channels = self._waveform_area.count() - 1
        cw.deleteLater()
        self._waveform_area.setFixedHeight(num_channels * Waveform.PREF_HEIGHT)
        self._waveform_area.refresh()

    def _update_xrange(self):
        data = self._state["data"]
        logs = self._state["logs"]
        maximum = 0
        for d in data.values():
            if d is None or len(d) == 0:
                continue
            temp = d[-1][0]
            if maximum < temp:
                maximum = temp
        for d in logs.values():
            if d is None or len(d) == 0:
                continue
            temp = d[-1][0]
            if maximum < temp:
                maximum = temp
        self._ref_axis.setRange(xRange=(0, maximum))

    def _clear_plots(self):
        for i in reversed(range(self._waveform_area.count())):
            cw = self._waveform_area.widget(i)
            self._remove_plot(cw)

    def on_trace_update(self):
        for i in range(self._waveform_area.count()):
            cw = self._waveform_area.widget(i)
            cw.on_load_data()
            cw.on_load_logs()
        self._update_xrange()

    def on_cursor_moved(self, x):
        self._cursor_x_pos = x
        for i in range(self._waveform_area.count()):
            cw = self._waveform_area.widget(i)
            cw.on_cursor_moved(x)

    def _on_toggle_cursor(self):
        for i in range(self._waveform_area.count()):
            cw = self._waveform_area.widget(i)
            cw.on_set_cursor_visible(self._is_show_cursor)
        self._is_show_cursor = not self._is_show_cursor


class WaveformProxyClient:
    def __init__(self, state, loop):
        self._state = state
        self._loop = loop

        self.proxy_sub = None
        self.devices_sub = None
        self.rpc_client = AsyncioClient()

        self._proxy_addr = None
        self._proxy_port = None
        self._proxy_port_ctl = None
        self._on_sub_reconnect = asyncio.Event()
        self._on_rpc_reconnect = asyncio.Event()
        self._reconnect_rpc_task = None
        self._reconnect_sub_task = None

    async def pull_from_device_task(self):
        asyncio.ensure_future(exc_to_warning(self.rpc_client.pull_from_device()))

    def update_address(self, addr, port, port_control):
        self._proxy_addr = addr
        self._proxy_port = port
        self._proxy_port_ctl = port_control
        self._on_rpc_reconnect.set()
        self._on_sub_reconnect.set()

    # Proxy client connections
    async def start(self, server, port):
        self._reconnect_rpc_task = asyncio.ensure_future(self.reconnect_rpc(), loop=self._loop)
        self._reconnect_sub_task = asyncio.ensure_future(self.reconnect_sub(), loop=self._loop)
        try:
            await self.devices_sub.connect(server, port)
        except:
            logger.error("Failed to connect to master.", exc_info=True)

    async def reconnect_rpc(self):
        try:
            while True:
                await self._on_rpc_reconnect.wait()
                self._on_rpc_reconnect.clear()
                logger.info("Attempting coreanalyzer RPC connection...")
                try:
                    await self.rpc_client.connect_rpc(self._proxy_addr,
                                                      self._proxy_port_ctl,
                                                      "coreanalyzer_proxy_control")
                except:
                    logger.info("coreanalyzer RPC timed out, trying again...")
                    await asyncio.sleep(5)
                    self._on_rpc_reconnect.set()
                else:
                    logger.info("Connected RPC to core analyzer proxy.")
        except asyncio.CancelledError:
            pass

    async def reconnect_sub(self):
        try:
            while True:
                await self._on_sub_reconnect.wait()
                self._on_sub_reconnect.clear()
                logger.info("Attempting coreanalyzer subscriber connection...")
                try:
                    await self.proxy_sub.connect(self._proxy_addr, self._proxy_port)
                except:
                    logger.info("coreanalyzer subscriber timed out, trying again...")
                    await asyncio.sleep(5)
                    self._on_sub_reconnect.set()
                else:
                    logger.info("Connected subscriber to core analyzer proxy.")
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._reconnect_rpc_task.cancel()
        self._reconnect_sub_task.cancel()
        await asyncio.wait_for(self._reconnect_rpc_task, None)
        await asyncio.wait_for(self._reconnect_sub_task, None)
        try:
            await self.devices_sub.close()
            self.rpc_client.close_rpc()
            await self.proxy_sub.close()
        except:
            logger.error("Error occurred while closing proxy connections.", exc_info=1)


class _CursorTimeControl(QtWidgets.QLineEdit):
    submit = QtCore.pyqtSignal(float)
    PRECISION = 10

    def __init__(self, parent):
        QtWidgets.QLineEdit.__init__(self, parent=parent)
        self._value = 0
        self._val_to_text(0)
        self.textChanged.connect(self._text_to_val)
        self.returnPressed.connect(self._on_submit)

    def _text_to_val(self, text):
        try:
            self._value = pg.siEval(text)
        except:
            pass

    def _val_to_text(self, val):
        self.setText(pg.siFormat(val, suffix="s", allowUnicode=False, precision=self.PRECISION))

    def _on_submit(self):
        self.submit.emit(self._value)
        self._val_to_text(self._value)
        self.clearFocus()

    def set_time(self, t):
        self._val_to_text(t)


class WaveformDock(QtWidgets.QDockWidget):
    traceDataChanged = QtCore.pyqtSignal()

    def __init__(self, loop=None):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(
            QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable)

        self._state = {
            "timescale": None,
            "logs": dict(),
            "data": dict(),
            "dump": None,
            "decoded_dump": None,
            "ddb": dict(),
            "channels": list()
        }

        self._current_dir = "c://"

        self.proxy_client = WaveformProxyClient(self._state, loop)
        analyzer_proxy_sub = Subscriber("coreanalyzer_proxy_pubsub",
                                        self.init_dump, self.update_dump)
        devices_sub = Subscriber("devices", self.init_ddb, self.update_ddb)
        self.proxy_client.proxy_sub = analyzer_proxy_sub
        self.proxy_client.devices_sub = devices_sub

        self._decoder_lock = asyncio.Lock()

        grid = LayoutWidget()
        self.setWidget(grid)

        self._menu_btn = QtWidgets.QPushButton()
        self._menu_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogStart))
        grid.addWidget(self._menu_btn, 0, 0)

        self._pull_btn = QtWidgets.QToolButton()
        self._pull_btn.setToolTip("Pull device buffer")
        self._pull_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_BrowserReload))
        grid.addWidget(self._pull_btn, 0, 1)
        self._pull_btn.clicked.connect(
            lambda: asyncio.ensure_future(self.proxy_client.pull_from_device_task()))

        self._cursor_control = _CursorTimeControl(self)
        grid.addWidget(self._cursor_control, 0, 3, colspan=3)

        self._waveform_area = WaveformArea(self, self._state)
        self.traceDataChanged.connect(lambda: self._waveform_area.on_trace_update())
        self._cursor_control.submit.connect(self._waveform_area.on_cursor_moved)
        self._waveform_area.cursorMoved.connect(self._cursor_control.set_time)
        grid.addWidget(self._waveform_area, 2, 0, colspan=12)

        self._add_btn = QtWidgets.QToolButton()
        self._add_btn.setToolTip("Add channels...")
        self._add_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogListView))
        grid.addWidget(self._add_btn, 0, 2)
        self._add_btn.clicked.connect(self._waveform_area.add_plots_dialog)

        self._file_menu = QtWidgets.QMenu()
        self._add_async_action("Open trace...", self.open_trace)
        self._add_async_action("Save trace...", self.save_trace)
        self._add_async_action("Save VCD...", self.save_vcd)
        self._menu_btn.setMenu(self._file_menu)

    def _add_async_action(self, label, coro):
        action = QtWidgets.QAction(label, self)
        action.triggered.connect(lambda: asyncio.ensure_future(exc_to_warning(coro())))
        self._file_menu.addAction(action)

    async def update_from_dump(self, dump):
        async with self._decoder_lock:
            ddb = self._state["ddb"]
            decoded_dump = await async_decode_dump(dump)
            trace = await async_decoded_dump_to_waveform(ddb, decoded_dump)
            trace["dump"] = dump
            trace["decoded_dump"] = decoded_dump
            self._state.update(trace)
            logger.info("Core analyzer trace updated.")
            self.traceDataChanged.emit()

    # File IO
    async def open_trace(self):
        try:
            filename = await get_open_file_name(
                self,
                "Load Analyzer Trace",
                self._current_dir,
                "All files (*.*)")
        except asyncio.CancelledError:
            return
        self._current_dir = os.path.dirname(filename)
        try:
            with open(filename, 'rb') as f:
                dump = f.read()
            await self.update_from_dump(dump)
        except:
            logger.error("Failed to open analyzer trace.",
                         exc_info=True)

    async def save_trace(self):
        dump = self._state["dump"]
        try:
            filename = await get_save_file_name(
                self,
                "Save Analyzer Trace",
                self._current_dir,
                "All files (*.*)")
        except asyncio.CancelledError:
            return
        self._current_dir = os.path.dirname(filename)
        try:
            with open(filename, 'wb') as f:
                f.write(dump)
        except:
            logger.error("Failed to save analyzer trace.",
                         exc_info=True)

    async def save_vcd(self):
        ddb = self._state["ddb"]
        decoded_dump = self._state["decoded_dump"]
        try:
            filename = await get_save_file_name(
                self,
                "Save VCD",
                self._current_dir,
                "All files (*.*)")
        except asyncio.CancelledError:
            return
        self._current_dir = os.path.dirname(filename)
        try:
            with open(filename, 'w') as f:
                await async_decoded_dump_to_vcd(f, ddb, decoded_dump)
        except:
            logger.error("Failed to save to VCD.",
                         exc_info=True)
        finally:
            logger.info("Finished writing to VCD.")

    # Proxy subscriber callbacks
    def init_dump(self, dump):
        return dump

    def update_dump(self, mod):
        dump = mod.get("value", None)
        if dump:
            asyncio.ensure_future(self.update_from_dump(dump))

    # DeviceDB subscriber callbacks
    def init_ddb(self, ddb):
        setitem(self._state, "ddb", ddb)

    def update_ddb(self, mod):
        devices = self._state["ddb"]
        addr = None
        self._state["channels"].clear()
        self._state["channels"].extend(get_channel_list(devices))
        for name, desc in devices.items():
            if isinstance(desc, dict):
                if desc["type"] == "controller" and name == "core_analyzer":
                    addr = desc["host"]
                    port = desc.get("port_proxy", 1382)
                    port_control = desc.get("port_proxy_control", 1385)
        if addr is not None:
            self.proxy_client.update_address(addr, port, port_control)

    def ccb_notify(self, message):
        try:
            service = message["service"]
            if service == "pull_trace_from_device":
                asyncio.ensure_future(
                    exc_to_warning(self.proxy_client.rpc_client.pull_from_device()))
        except:
            logger.error("failed to process CCB", exc_info=True)
