from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient

from artiq.tools import exc_to_warning
from artiq.gui.tools import LayoutWidget, get_open_file_name, get_save_file_name
from artiq.gui.models import DictSyncTreeSepModel, LocalModelManager
from artiq.gui.dndwidgets import DragDropSplitter, VDragScrollArea
from artiq.coredevice import comm_analyzer
import os
import numpy as np
from operator import setitem
from enum import Enum
import pyqtgraph as pg
import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class Model(DictSyncTreeSepModel):
    def __init__(self, init):
        DictSyncTreeSepModel.__init__(self, "/", ["Channels"], init)


class _AddChannelDialog(QtWidgets.QDialog):
    accepted = QtCore.pyqtSignal(list)

    def __init__(self, parent, channels_mgr, title):
        QtWidgets.QDialog.__init__(self, parent=parent)

        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.setWindowTitle("Add " + title)
        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)
        self._channels_widget = QtWidgets.QTreeView()
        self._channels_widget.setHeaderHidden(True)
        self._channels_widget.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectItems)
        self._channels_widget.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self._model = Model(dict())
        channels_mgr.add_setmodel_callback(self.set_model)
        grid.addWidget(self._channels_widget, 0, 0, 1, 2)

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

    def set_model(self, model):
        self._model = model
        self._channels_widget.setModel(model)

    def add_channels(self):
        selection = self._channels_widget.selectedIndexes()
        channels = []
        for select in selection:
            key = self._model.index_to_key(select)
            if key is not None:
                width = self._model[key].ref
                channels.append((key, width))
        self.accepted.emit(channels)
        self.close()


class Waveform(pg.PlotWidget):
    MIN_HEIGHT = 50
    MAX_HEIGHT = 200
    PREF_HEIGHT = 100

    cursorMoved = QtCore.pyqtSignal(float)

    def __init__(self, channel, state, parent=None):
        pg.PlotWidget.__init__(self, parent=parent, x=None, y=None)
        self.setMinimumHeight(Waveform.MIN_HEIGHT)
        self.setMaximumHeight(Waveform.MAX_HEIGHT)
        self.setMenuEnabled(False)

        self.name = channel[0]
        self.width = channel[1][0]

        self.state = state
        self.x_data = []
        self.y_data = []
        self._is_show_cursor = True

        self.plotItem = self.getPlotItem()
        self.plotItem.setRange(yRange=(0, 1), padding=0.1)
        self.plotItem.hideButtons()
        self.plotItem.getAxis("bottom").setStyle(showValues=False, tickLength=0)
        self.plotItem.hideAxis("top")

        self.plotDataItem = self.plotItem.listDataItems()[0]
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
        self.plotDataItem.opts.update(pdi_opts)

        self.viewBox = self.plotItem.getViewBox()
        self.viewBox.setMouseEnabled(x=True, y=False)
        self.viewBox.disableAutoRange(axis=pg.ViewBox.YAxis)
        self.viewBox.setLimits(xMin=0)

        self.plotItem.getAxis("left").setStyle(showValues=False, tickLength=0)
        self.plotItem.setRange(yRange=(0, 1), padding=0.1)

        self.cursor = pg.InfiniteLine()
        self.cursorY = 0
        self.addItem(self.cursor)

        self.cursor_label = pg.TextItem()
        self.addItem(self.cursor_label)

        self.title_label = pg.TextItem(self.name)
        self.addItem(self.title_label)
        self.viewBox.sigRangeChanged.connect(self.on_frame_moved)
        self.viewBox.sigTransformChanged.connect(self.on_frame_moved)

        self.setContextMenuPolicy(QtCore.Qt.ActionsContextMenu)

    def on_frame_moved(self):
        value_pos = self.viewBox.mapSceneToView(QtCore.QPoint(0, 0))
        title_pos = self.viewBox.mapSceneToView(QtCore.QPoint(0, self.height() // 2))
        self.cursor_label.setPos(value_pos)
        self.title_label.setPos(title_pos)

    def update_x_max(self):
        self.viewBox.setLimits(xMax=self.state["stopped_x"])

    def on_set_cursor_visible(self, visible):
        if visible:
            self.removeItem(self.cursor)
            self._is_show_cursor = False
        else:
            self.addItem(self.cursor)
            self._is_show_cursor = True

    def on_cursor_moved(self, x):
        self.cursor.setValue(x)
        if len(self.x_data) < 1:
            return
        ind = np.searchsorted(self.x_data, x, side="left") - 1
        dr = self.plotDataItem.dataRect()
        if dr is not None and dr.left() <= x <= dr.right() \
                and 0 <= ind < len(self.y_data):
            self.cursorY = self.y_data[ind]
        else:
            self.cursorY = 0
        self.refresh_cursor_label()

    def on_load_data(self):
        raise NotImplementedError

    def refresh_cursor_label(self):
        raise NotImplementedError

    # override
    def mouseMoveEvent(self, e):
        if e.buttons() == QtCore.Qt.LeftButton \
           and e.modifiers() == QtCore.Qt.ControlModifier:
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
        pos = self.viewBox.mapSceneToView(e.pos())
        self.cursorMoved.emit(pos.x())

    # override
    def wheelEvent(self, e):
        if e.modifiers() & QtCore.Qt.ShiftModifier:
            super().wheelEvent(e)


class LogWaveform(Waveform):
    def __init__(self, channel, state, parent=None):
        Waveform.__init__(self, channel, state, parent)

    def on_load_data(self):
        try:
            self.x_data, self.y_data = zip(*self.state['logs'][self.name])
            self.plotDataItem.setData(x=self.x_data, y=np.ones(len(self.x_data)))
            self.plotDataItem.opts.update({"connect": np.zeros(2), "symbol": "x"})
            old_msg = ""
            old_x = 0
            for x, msg in zip(self.x_data, self.y_data):
                if x == old_x: 
                    old_msg += "\n" + msg
                else:
                    lbl = pg.TextItem(old_msg)
                    self.addItem(lbl)
                    lbl.setPos(old_x, 1)
                    old_msg = msg
                    old_x = x
            lbl = pg.TextItem(old_msg)
            self.addItem(lbl)
            lbl.setPos(old_x, 1)
        except:
            logger.debug('unable to load data', exc_info=True)
            self.plotDataItem.setData(x=[0], y=[0])

    def refresh_cursor_label(self):
        self.cursor_label.setText("")


class TTLWaveform(Waveform):
    def __init__(self, channel, state, parent=None):
        Waveform.__init__(self, channel, state, parent)

    def on_load_data(self):
        try:
            self.x_data, self.y_data = zip(*self.state['data'][self.name])
            display_x, display_y = [], []
            previous_y = 0
            for x, y in zip(self.x_data, self.y_data):
                state_unchanged = previous_y == y
                if state_unchanged:
                    arw = pg.ArrowItem(pxMode=True, angle=90)
                    self.addItem(arw)
                    arw.setPos(x, 1)
                previous_y = y
            self.plotDataItem.setData(x=self.x_data, y=self.y_data)
        except:
            logger.debug('unable to load data', exc_info=True)
            self.plotDataItem.setData(x=[0], y=[0])

    def refresh_cursor_label(self):
        lbl = str(self.cursorY)
        self.cursor_label.setText(lbl)


class DigitalWaveform(Waveform):
    def __init__(self, channel, state, parent=None):
        Waveform.__init__(self, channel, state, parent)
        self._labels = []
        self.viewBox.sigTransformChanged.connect(self._update_labels)
        self.plotDataItem.opts.update({"downsample": 1, "autoDownsample": False})
        self._secondaryDataItem = self.plotItem.plot(x=[], y=[], pen='r')

    def _update_labels(self):
        for i in range(len(self.x_data) - 1): 
            x1, x2 = self.x_data[i], self.x_data[i+1]
            lbl = self._labels[i]
            bounds = lbl.boundingRect()
            bounds_view = self.viewBox.mapSceneToView(bounds)
            if bounds_view.boundingRect().width() < x2 - x1:
                lbl.setText(str(hex(self.y_data[i])))
            else:
                lbl.setText("\n\n+")

    def on_load_data(self):
        try:
            self.x_data, self.y_data = zip(*self.state['data'][self.name])
            display_x, display_y = [], []
            for x, y in zip(self.x_data, self.y_data):
                if y is not None and y != 0:
                        display_x += [x, x]
                        display_y += [0, 1]
                        display_x.append(x)
                        display_y.append(1)
                else:
                    display_x.append(x)
                    display_y.append(y)
                lbl = pg.TextItem(str(hex(y)), anchor=(0, 0.5))
                self.addItem(lbl)
                lbl.setPos(x, 0.5)
                lbl.setTextWidth(100)
                self._labels.append(lbl)
            self.plotDataItem.setData(x=display_x, y=display_y)
            self._secondaryDataItem.setData(x=[self.x_data[0], self.x_data[-1]], y=[0, 0])
        except:
            logger.debug('unable to load data', exc_info=True)
            self.plotDataItem.setData(x=[0], y=[0])

    def refresh_cursor_label(self):
        lbl = str(hex(self.cursorY))
        self.cursor_label.setText(lbl)


class AnalogWaveform(Waveform):
    def __init__(self, channel, state, parent=None):
        Waveform.__init__(self, channel, state, parent)

    def on_load_data(self):
        try:
            self.x_data, self.y_data = zip(*self.state['data'][self.name])
            self.plotDataItem.setData(x=self.x_data, y=self.y_data)
            mx = max(self.y_data)
            mn = min(self.y_data)
            self.plotItem.setRange(yRange=(mn, mx), padding=0.1)
        except:
            logger.debug('unable to load data', exc_info=True)
            self.plotDataItem.setData(x=[0], y=[0])

    def refresh_cursor_label(self):
        lbl = str(self.cursorY)
        self.cursor_label.setText(lbl)


class WaveformArea(QtWidgets.QWidget):
    cursorMoved = QtCore.pyqtSignal(float)

    def __init__(self, parent, state, channels_mgr, log_channels_mgr):
        QtWidgets.QWidget.__init__(self, parent=parent)
        self._state = state
        self._channels_mgr = channels_mgr
        self._log_channels_mgr = log_channels_mgr

        self._is_show_cursor = True
        self._cursor_x_pos = 0

        layout = QtWidgets.QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.setLayout(layout)

        self._ref_axis = pg.PlotWidget()
        self._ref_axis.hideAxis("bottom")
        self._ref_axis.hideAxis("left")
        self._ref_axis.hideButtons()
        self._ref_axis.setFixedHeight(45)
        self._ref_axis.setMenuEnabled(False)
        top = pg.AxisItem("top")
        top.setLabel("", units="s")
        self._ref_axis.setAxisItems({"top": top})

        self._ref_vb = self._ref_axis.getPlotItem().getViewBox()
        self._ref_vb.setFixedHeight(0)
        self._ref_vb.setMouseEnabled(x=True, y=False)
        self._ref_vb.setLimits(xMin=0)

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

    def _add_plot(self, channel, waveform_type):
        num_channels = self._waveform_area.count()
        self._waveform_area.setFixedHeight((num_channels + 1) * Waveform.PREF_HEIGHT)
        cw = waveform_type(channel, self._state, parent=self._waveform_area)
        cw.cursorMoved.connect(lambda x: self.on_cursor_moved(x))
        cw.cursorMoved.connect(self.cursorMoved.emit)
        self._add_waveform_actions(cw)
        cw.setXLink(self._ref_vb)
        cw.getPlotItem().showGrid(x=True, y=True)
        self._waveform_area.addWidget(cw)
        cw.on_load_data()
        cw.on_cursor_moved(self._cursor_x_pos)
        cw.update_x_max()

    async def _add_plots_dialog_task(self, manager, title, is_log):
        dialog = _AddChannelDialog(self, manager, title)
        fut = asyncio.Future()

        def on_accept(s):
            fut.set_result(s)
        dialog.accepted.connect(on_accept)
        dialog.open()
        channels = await fut
        if is_log:
            for channel in channels:
                self._add_plot(channel, LogWaveform)
        else:
            for channel in channels:
                ty = channel[1][1]
                waveform_type = {
                    "ttl": TTLWaveform,
                    "digital": DigitalWaveform,
                    "analog": AnalogWaveform
                }[ty]
                self._add_plot(channel, waveform_type)

    def add_plots_dialog(self):
        args = [self._channels_mgr,
                "channels",
                False]
        asyncio.ensure_future(exc_to_warning(self._add_plots_dialog_task(*args)))

    def add_log_plots_dialog(self):
        args = [self._log_channels_mgr,
                "logs",
                True]
        asyncio.ensure_future(exc_to_warning(self._add_plots_dialog_task(*args)))

    def _remove_plot(self, cw):
        num_channels = self._waveform_area.count() - 1
        cw.deleteLater()
        self._waveform_area.setFixedHeight(num_channels * Waveform.PREF_HEIGHT)
        self._waveform_area.refresh()

    def _clear_plots(self):
        for i in reversed(range(self._waveform_area.count())):
            cw = self._waveform_area.widget(i)
            self._remove_plot(cw)

    def on_trace_update(self):
        for i in range(self._waveform_area.count()):
            cw = self._waveform_area.widget(i)
            cw.on_load_data()
            cw.on_cursor_moved(self._cursor_x_pos)
            cw.update_x_max()
        maximum = self._state["stopped_x"]
        self._ref_axis.setLimits(xMax=maximum)
        self._ref_axis.setRange(xRange=(0, maximum))

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

        self.devices_sub = None
        self.rpc_client = AsyncioClient()
        self.proxy_receiver = None

        self._proxy_addr = None
        self._proxy_port = None
        self._proxy_port_ctl = None
        self._on_sub_reconnect = asyncio.Event()
        self._on_rpc_reconnect = asyncio.Event()
        self._reconnect_rpc_task = None
        self._reconnect_receiver_task = None

    async def trigger_proxy_task(self):
        try:
            if self.rpc_client.get_rpc_id()[0] is None:
                raise AttributeError("Unable to identify RPC target. Is analyzer proxy connected?")
            asyncio.ensure_future(self.rpc_client.trigger())
        except Exception as e:
            logger.warning("Failed to pull from device: %s", e)

    def update_address(self, addr, port, port_control):
        self._proxy_addr = addr
        self._proxy_port = port
        self._proxy_port_ctl = port_control
        self._on_rpc_reconnect.set()
        self._on_sub_reconnect.set()

    # Proxy client connections
    async def start(self, server, port):
        try:
            await self.devices_sub.connect(server, port)
            self._reconnect_rpc_task = asyncio.ensure_future(
                self.reconnect_rpc(), loop=self._loop)
            self._reconnect_receiver_task = asyncio.ensure_future(
                self.reconnect_receiver(), loop=self._loop)
        except Exception as e:
            logger.error("Failed to connect to master: %s", e)

    async def reconnect_rpc(self):
        try:
            while True:
                await self._on_rpc_reconnect.wait()
                self._on_rpc_reconnect.clear()
                logger.info("Attempting analyzer proxy RPC connection...")
                try:
                    await self.rpc_client.connect_rpc(self._proxy_addr,
                                                      self._proxy_port_ctl,
                                                      "coreanalyzer_proxy_control")
                except Exception:
                    logger.info("Analyzer proxy RPC timed out, trying again...")
                    await asyncio.sleep(5)
                    self._on_rpc_reconnect.set()
                else:
                    logger.info("RPC connected to analyzer proxy on %s/%s", self._proxy_addr, self._proxy_port_ctl)
        except asyncio.CancelledError:
            pass

    async def reconnect_receiver(self):
        try:
            while True:
                await self._on_sub_reconnect.wait()
                self._on_sub_reconnect.clear()
                logger.info("Setting up analyzer proxy receiver...")
                try:
                    await self.proxy_receiver.connect(self._proxy_addr, self._proxy_port)
                except Exception:
                    logger.info("Failed to set up analyzer proxy receiver, reconnecting...")
                    await asyncio.sleep(5)
                    self._on_sub_reconnect.set()
                else:
                    logger.info("Receiving from analyzer proxy on %s:%s", self._proxy_addr, self._proxy_port)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        try:
            self._reconnect_rpc_task.cancel()
            self._reconnect_receiver_task.cancel()
            await asyncio.wait_for(self._reconnect_rpc_task, None)
            await asyncio.wait_for(self._reconnect_receiver_task, None)
            await self.devices_sub.close()
            self.rpc_client.close_rpc()
            await self.proxy_receiver.close()
        except Exception as e:
            logger.error("Error occurred while closing proxy connections: %s", e, exc_info=True)


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
        except Exception:
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

        self._channels_mgr = LocalModelManager(Model)
        self._channels_mgr.init({})

        self._log_channels_mgr = LocalModelManager(Model)
        self._log_channels_mgr.init({})

        self._state = {
            "timescale": None,
            "stopped_x": None,
            "logs": dict(),
            "data": dict(),
            "dump": None,
            "decoded_dump": None,
            "ddb": dict(),
        }

        self._current_dir = "c://"

        self.proxy_client = WaveformProxyClient(self._state, loop)
        devices_sub = Subscriber("devices", self.init_ddb, self.update_ddb)

        self.queue = asyncio.Queue(maxsize=5)
        proxy_receiver = comm_analyzer.AnalyzerProxyReceiver(self.on_dump_receive)
        self.proxy_client.devices_sub = devices_sub
        self.proxy_client.proxy_receiver = proxy_receiver

        grid = LayoutWidget()
        self.setWidget(grid)

        self._menu_btn = QtWidgets.QPushButton()
        self._menu_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogStart))
        grid.addWidget(self._menu_btn, 0, 0)

        self._request_dump_btn = QtWidgets.QToolButton()
        self._request_dump_btn.setToolTip("Request dump")
        self._request_dump_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_BrowserReload))
        grid.addWidget(self._request_dump_btn, 0, 1)
        self._request_dump_btn.clicked.connect(
            lambda: asyncio.ensure_future(self.proxy_client.trigger_proxy_task()))

        self._waveform_area = WaveformArea(self, self._state,
                                           self._channels_mgr,
                                           self._log_channels_mgr)
        self.traceDataChanged.connect(self._waveform_area.on_trace_update)
        self.traceDataChanged.connect(self._update_log_channels)
        grid.addWidget(self._waveform_area, 2, 0, colspan=12)

        self._add_btn = QtWidgets.QToolButton()
        self._add_btn.setToolTip("Add channels...")
        self._add_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogListView))
        grid.addWidget(self._add_btn, 0, 2)
        self._add_btn.clicked.connect(self._waveform_area.add_plots_dialog)

        self._add_logs_btn = QtWidgets.QToolButton()
        self._add_logs_btn.setToolTip("Add logs...")
        self._add_logs_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogListView))
        grid.addWidget(self._add_logs_btn, 0, 3)
        self._add_logs_btn.clicked.connect(self._waveform_area.add_log_plots_dialog)

        self._cursor_control = _CursorTimeControl(self)
        grid.addWidget(self._cursor_control, 0, 4, colspan=3)
        self._cursor_control.submit.connect(self._waveform_area.on_cursor_moved)
        self._waveform_area.cursorMoved.connect(self._cursor_control.set_time)

        self._file_menu = QtWidgets.QMenu()
        self._add_async_action("Open trace...", self.open_trace)
        self._add_async_action("Save trace...", self.save_trace)
        self._add_async_action("Save VCD...", self.save_vcd)
        self._menu_btn.setMenu(self._file_menu)

    def _add_async_action(self, label, coro):
        action = QtWidgets.QAction(label, self)
        action.triggered.connect(lambda: asyncio.ensure_future(exc_to_warning(coro())))
        self._file_menu.addAction(action)

    def _update_log_channels(self):
        self._log_channels_mgr.init(self._state['logs'])

    def on_dump_receive(self, *args):
        header = comm_analyzer.decode_header_from_receiver(*args)
        decoded_dump = comm_analyzer.decode_dump_loop(*header)
        ddb = self._state['ddb']
        trace = comm_analyzer.decoded_dump_to_waveform(ddb, decoded_dump)
        self._state.update(trace)
        self.traceDataChanged.emit()

    def on_dump_read(self, dump):
        decoded_dump = comm_analyzer.decode_dump(dump)
        ddb = self._state['ddb']
        trace = comm_analyzer.decoded_dump_to_waveform(ddb, decoded_dump)
        self._state.update(trace)
        self.traceDataChanged.emit()

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
            self.on_dump_read(dump) 
        except Exception as e:
            logger.error("Failed to open analyzer trace: %s", e)

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

        except Exception as e:
            logger.error("Failed to save analyzer trace: %s", e)

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
                await comm_analyer.async_decoded_dump_to_vcd(f, ddb, decoded_dump)
        except Exception as e:
            logger.error("Faile to save as VCD: %s", e)
        finally:
            logger.info("Finished writing to VCD.")

    # DeviceDB subscriber callbacks
    def init_ddb(self, ddb):
        setitem(self._state, "ddb", ddb)

    def update_ddb(self, mod):
        devices = self._state["ddb"]
        addr = None
        self._channels_mgr.init(comm_analyzer.get_channel_list(devices))
        for name, desc in devices.items():
            if isinstance(desc, dict):
                if desc["type"] == "controller" and name == "core_analyzer":
                    addr = desc["host"]
                    port = desc.get("port_proxy", 1385)
                    port_control = desc.get("port_proxy_control", 1386)
        if addr is not None:
            self.proxy_client.update_address(addr, port, port_control)
