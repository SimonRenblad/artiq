import os
import asyncio
import logging

from PyQt5 import QtCore, QtWidgets, QtGui
from PyQt5.QtCore import Qt

from sipyco.sync_struct import Subscriber
from sipyco.pc_rpc import AsyncioClient

from artiq.tools import exc_to_warning
from artiq.coredevice import comm_analyzer
from artiq.coredevice.comm_analyzer import WaveformType
from artiq.gui.tools import LayoutWidget, get_open_file_name
from artiq.gui.models import DictSyncTreeSepModel, LocalModelManager


logger = logging.getLogger(__name__)


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
                    logger.error("Error caught when disconnecting proxy client.", exc_info=True)
                try:
                    await self.reconnect_cr()
                except Exception:
                    logger.error(
                        "Error caught when reconnecting proxy client. Retrying...", exc_info=True)
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
            logger.error("Error caught while closing proxy client.", exc_info=True)

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


class WaveformDock(QtWidgets.QDockWidget):
    def __init__(self):
        QtWidgets.QDockWidget.__init__(self, "Waveform")
        self.setObjectName("Waveform")
        self.setFeatures(
            QtWidgets.QDockWidget.DockWidgetMovable | QtWidgets.QDockWidget.DockWidgetFloatable)

        self._channel_model = Model({})

        self._ddb = None

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
        grid.addWidget(self._add_btn, 0, 2)

        self._file_menu = QtWidgets.QMenu()
        self._add_async_action("Open trace...", self.load_trace)
        self._menu_btn.setMenu(self._file_menu)

    def _add_async_action(self, label, coro):
        action = QtWidgets.QAction(label, self)
        action.triggered.connect(
            lambda: asyncio.ensure_future(exc_to_warning(coro())))
        self._file_menu.addAction(action)

    def on_dump_receive(self, dump):
        decoded_dump = comm_analyzer.decode_dump(dump)
        waveform_data = comm_analyzer.decoded_dump_to_waveform_data(self._ddb, decoded_dump)
        self._waveform_data.update(waveform_data)
        for log in self._waveform_data['logs']:
            self._channel_model[log] = (0, WaveformType.LOG)

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
            logger.error("Failed to open analyzer trace.", exc_info=True)

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
