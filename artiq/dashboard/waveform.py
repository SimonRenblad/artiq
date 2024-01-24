import os
import asyncio
import logging

from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

import pyqtgraph as pg

from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient

from artiq.tools import exc_to_warning
from artiq.coredevice import comm_analyzer
from artiq.coredevice.comm_analyzer import WaveformType
from artiq.gui.tools import LayoutWidget, get_open_file_name, get_save_file_name
from artiq.gui.models import DictSyncTreeSepModel, LocalModelManager
from artiq.gui.dndwidgets import VDragScrollArea, VDragDropSplitter


logger = logging.getLogger(__name__)

WAVEFORM_MIN_HEIGHT = 50
WAVEFORM_MAX_HEIGHT = 200


class _BaseProxyClient:
    def __init__(self):
        self.addr = None
        self.port = None
        self._reconnect_event = asyncio.Event()
        self._reconnect_task = None

    async def start(self):
        self._reconnect_task = asyncio.ensure_future(
            exc_to_warning(self._reconnect()))

    def update_address(self, addr, port):
        self.addr = addr
        self.port = port
        self._reconnect_event.set()

    async def _reconnect(self):
        try:
            while True:
                await self._reconnect_event.wait()
                self._reconnect_event.clear()
                try:
                    await self.disconnect_cr()
                except:
                    logger.error("Error caught when disconnecting proxy client", exc_info=True)
                try:
                    await self.reconnect_cr()
                except Exception:
                    logger.error(
                        "Error caught when reconnecting proxy client, retrying...", exc_info=True)
                    await asyncio.sleep(5)
                    self._reconnect_event.set()
        except asyncio.CancelledError:
            pass

    async def close(self):
        try:
            self._reconnect_task.cancel()
            await asyncio.wait_for(self._reconnect_task, None)
            await self.disconnect_cr()
        except:
            logger.error("Error caught while closing proxy client", exc_info=True)

    async def reconnect_cr(self):
        raise NotImplementedError

    async def disconnect_cr(self):
        raise NotImplementedError


class RPCProxyClient(_BaseProxyClient):
    def __init__(self):
        _BaseProxyClient.__init__(self)
        self.client = AsyncioClient()

    async def trigger_proxy_task(self):
        if self.client.get_rpc_id()[0] is None:
            raise AttributeError("Unable to identify RPC target. Is analyzer proxy connected?")
        await self.client.trigger()

    async def reconnect_cr(self):
        await self.client.connect_rpc(self.addr,
                                      self.port,
                                      "coreanalyzer_proxy_control")

    async def disconnect_cr(self):
        self.client.close_rpc()


class ReceiverProxyClient(_BaseProxyClient):
    def __init__(self, receiver):
        _BaseProxyClient.__init__(self)
        self.receiver = receiver

    async def reconnect_cr(self):
        await self.receiver.connect(self.addr, self.port)

    async def disconnect_cr(self):
        await self.receiver.close()


class _WaveformView(QtWidgets.QWidget):
    def __init__(self, parent):
        QtWidgets.QWidget.__init__(self, parent=parent)

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
        self._top = pg.AxisItem("top")
        self._top.setScale(1e-12)
        self._top.setLabel(units="s")
        self._ref_axis.setAxisItems({"top": self._top})
        layout.addWidget(self._ref_axis)

        self._ref_vb = self._ref_axis.getPlotItem().getViewBox()
        self._ref_vb.setFixedHeight(0)
        self._ref_vb.setMouseEnabled(x=True, y=False)
        self._ref_vb.setLimits(xMin=0)

        scroll_area = VDragScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setContentsMargins(0, 0, 0, 0)
        scroll_area.setFrameShape(QtWidgets.QFrame.NoFrame)
        layout.addWidget(scroll_area)

        self._splitter = VDragDropSplitter(parent=scroll_area)
        self._splitter.setHandleWidth(1)
        scroll_area.setWidget(self._splitter)

    def setModel(self, model):
        self._model = model
        self._model.dataChanged.connect(self.onDataChange)
        self._model.rowsInserted.connect(self.onInsert)
        self._model.rowsRemoved.connect(self.onRemove)
        self._model.rowsMoved.connect(self.onMove)
        self._splitter.dropped.connect(self._model.move)

    def setTimescale(self, timescale):
        self._timescale = timescale
        self._top.setScale(1e-12 * timescale)
        for i in range(self._model.rowCount()):
            self._splitter.widget(i).setTimescale(timescale)

    def setStoppedX(self, stopped_x):
        self._stopped_x = stopped_x
        for i in range(self._model.rowCount()):
            self._splitter.widget(i).setStoppedX(stopped_x)

    def onDataChange(self, top, bottom, roles):
        first = top.row()
        last = bottom.row()
        for i in range(first, last + 1):
            data = self._model.data(self._model.index(i, 3))
            self._splitter.widget(i).onDataChange(data)

    def onInsert(self, parent, first, last):
        for i in range(first, last + 1):
            w = self._create_waveform(i)
            self._splitter.insertWidget(i, w)
        self._resize()

    def onRemove(self, parent, first, last):
        for i in reversed(range(first, last + 1)):
            w = self._splitter.widget(i)
            w.deleteLater()
        self._splitter.refresh()
        self._resize()

    def onMove(self, src_parent, src_start, src_end, dest_parent, dest_row):
        w = self._splitter.widget(src_start)
        self._splitter.insertWidget(dest_row, w)

    def _create_waveform(self, row):
        raise NotImplementedError

    def _resize(self):
        self._splitter.setFixedHeight(
            int((WAVEFORM_MIN_HEIGHT + WAVEFORM_MAX_HEIGHT) * self._model.rowCount() / 2))


class _WaveformModel(QtCore.QAbstractTableModel):
    def __init__(self):
        self.backing_struct = []
        self.headers = ["name", "type", "width", "data"]
        QtCore.QAbstractTableModel.__init__(self)

    def rowCount(self, parent=QtCore.QModelIndex()):
        return len(self.backing_struct)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return len(self.headers)

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if index.isValid():
            return self.backing_struct[index.row()][index.column()]
        return None

    def extend(self, data):
        length = len(self.backing_struct)
        len_data = len(data)
        self.beginInsertRows(QtCore.QModelIndex(), length, length + len_data - 1)
        self.backing_struct.extend(data)
        self.endInsertRows()

    def pop(self, row):
        self.beginRemoveRows(QtCore.QModelIndex(), row, row)
        self.backing_struct.pop(row)
        self.endRemoveRows()

    def move(self, src, dest):
        if src == dest:
            return
        if src < dest:
            dest, src = src, dest
        self.beginMoveRows(QtCore.QModelIndex(), src, src, QtCore.QModelIndex(), dest)
        self.backing_struct.insert(dest, self.backing_struct.pop(src))
        self.endMoveRows()

    def update_data(self, waveform_data, top, bottom):
        name_col = self.headers.index("name")
        data_col = self.headers.index("data")
        for i in range(top, bottom):
            name = self.data(self.index(i, name_col))
            if name in waveform_data:
                self.backing_struct[i][data_col] = waveform_data[name]
                self.dataChanged.emit(self.index(i, data_col),
                                      self.index(i, data_col))

    def update_all(self, waveform_data):
        self.update_data(waveform_data, 0, self.rowCount())


class Model(DictSyncTreeSepModel):
    def __init__(self, init):
        DictSyncTreeSepModel.__init__(self, "/", ["Channels"], init)

    def clear(self):
        for k in self.backing_store:
            self._del_item(self, k.split(self.separator))
        self.backing_store.clear()

    def update(self, d):
        for k, v in d.items():
            self[k] = v


class _AddChannelDialog(QtWidgets.QDialog):
    accepted = QtCore.pyqtSignal(list)

    def __init__(self, parent, model):
        QtWidgets.QDialog.__init__(self, parent=parent)
        self.setContextMenuPolicy(Qt.ActionsContextMenu)
        self.setWindowTitle("Add channels")

        grid = QtWidgets.QGridLayout()
        self.setLayout(grid)

        self._model = model
        self._tree_view = QtWidgets.QTreeView()
        self._tree_view.setHeaderHidden(True)
        self._tree_view.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectItems)
        self._tree_view.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self._tree_view.setModel(self._model)
        grid.addWidget(self._tree_view, 0, 0, 1, 2)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.close)
        cancel_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_DialogCancelButton))
        grid.addWidget(cancel_btn, 1, 0)
        confirm_btn = QtWidgets.QPushButton("Confirm")
        confirm_btn.clicked.connect(self.add_channels)
        confirm_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_DialogApplyButton))
        grid.addWidget(confirm_btn, 1, 1)

    def add_channels(self):
        selection = self._tree_view.selectedIndexes()
        channels = []
        for select in selection:
            key = self._model.index_to_key(select)
            if key is not None:
                width, ty = self._model[key].ref
                channels.append([key, width, ty, []])
        self.accepted.emit(channels)
        self.close()


class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(
            QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable)

        self._channel_model = Model({})
        self._waveform_model = _WaveformModel()

        self._ddb = None
        self._dump = None

        self._waveform_data = {
            "timescale": 1,
            "stopped_x": None,
            "logs": dict(),
            "data": dict(),
        }

        self._current_dir = os.getcwd()

        self.devices_sub = Subscriber("devices", self.init_ddb, self.update_ddb)
        self.rpc_client = RPCProxyClient()
        receiver = comm_analyzer.AnalyzerProxyReceiver(
            self.on_dump_receive)
        self.receiver_client = ReceiverProxyClient(receiver)

        grid = LayoutWidget()
        self.setWidget(grid)

        self._menu_btn = QtWidgets.QPushButton()
        self._menu_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogStart))
        grid.addWidget(self._menu_btn, 0, 0)

        self._request_dump_btn = QtWidgets.QToolButton()
        self._request_dump_btn.setToolTip("Fetch analyzer data from device")
        self._request_dump_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_BrowserReload))
        self._request_dump_btn.clicked.connect(
            lambda: asyncio.ensure_future(exc_to_warning(self.rpc_client.trigger_proxy_task())))
        grid.addWidget(self._request_dump_btn, 0, 1)

        self._add_btn = QtWidgets.QToolButton()
        self._add_btn.setToolTip("Add channels...")
        self._add_btn.setIcon(
            QtWidgets.QApplication.style().standardIcon(
                QtWidgets.QStyle.SP_FileDialogListView))
        self._add_btn.clicked.connect(self.on_add_channel_click)
        grid.addWidget(self._add_btn, 0, 2)

        self._file_menu = QtWidgets.QMenu()
        self._add_async_action("Open trace...", self.load_trace)
        self._add_async_action("Save trace...", self.save_trace)
        self._add_async_action("Save trace as VCD...", self.save_vcd)
        self._menu_btn.setMenu(self._file_menu)

        self._waveform_view = _WaveformView(self)
        self._waveform_view.setModel(self._waveform_model)
        grid.addWidget(self._waveform_view, 1, 0, colspan=12)

    def _add_async_action(self, label, coro):
        action = QtWidgets.QAction(label, self)
        action.triggered.connect(
            lambda: asyncio.ensure_future(exc_to_warning(coro())))
        self._file_menu.addAction(action)

    async def _add_channel_task(self):
        dialog = _AddChannelDialog(self, self._channel_model)
        fut = asyncio.Future()

        def on_accept(s):
            fut.set_result(s)
        dialog.accepted.connect(on_accept)
        dialog.open()
        channels = await fut
        count = self._waveform_model.rowCount()
        self._waveform_model.extend(channels)
        self._waveform_model.update_data(self._waveform_data['data'],
                                         count,
                                         count + len(channels))

    def on_add_channel_click(self):
        asyncio.ensure_future(self._add_channel_task())

    def on_dump_receive(self, dump):
        self._dump = dump
        decoded_dump = comm_analyzer.decode_dump(dump)
        waveform_data = comm_analyzer.decoded_dump_to_waveform_data(self._ddb, decoded_dump)
        self._waveform_data.update(waveform_data)
        self._channel_model.update(self._waveform_data['logs'])
        self._waveform_model.update_all(self._waveform_data['data'])
        self._waveform_view.setStoppedX(self._waveform_data['stopped_x'])
        self._waveform_view.setTimescale(self._waveform_data['timescale'])

    async def load_trace(self):
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
            self.on_dump_receive(dump)
        except:
            logger.error("Failed to open analyzer trace", exc_info=True)

    async def save_trace(self):
        if self._dump is None:
            logger.error("No analyzer trace stored in dashboard, "
                         "try loading from file or fetching from device")
            return
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
                f.write(self._dump)
        except:
            logger.error("Failed to save analyzer trace", exc_info=True)

    async def save_vcd(self):
        if self._dump is None:
            logger.error("No analyzer trace stored in dashboard, "
                         "try loading from file or fetching from device")
            return
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
            decoded_dump = comm_analyzer.decode_dump(self._dump)
            with open(filename, 'w') as f:
                comm_analyzer.decoded_dump_to_vcd(f, self._ddb, decoded_dump)
        except:
            logger.error("Failed to save trace as VCD", exc_info=True)

    def _process_ddb(self):
        channel_list = comm_analyzer.get_channel_list(self._ddb)
        self._channel_model.clear()
        self._channel_model.update(channel_list)
        desc = self._ddb.get("core_analyzer")
        if desc is not None:
            addr = desc["host"]
            port_proxy = desc.get("port_proxy", 1385)
            port = desc.get("port", 1386)
            self.receiver_client.update_address(addr, port_proxy)
            self.rpc_client.update_address(addr, port)

    def init_ddb(self, ddb):
        self._ddb = ddb
        self._process_ddb()
        return ddb

    def update_ddb(self, mod):
        self._process_ddb()
