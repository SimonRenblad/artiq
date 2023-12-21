from operator import itemgetter
from collections import namedtuple
from itertools import count
from contextlib import contextmanager
from sipyco import keepalive
import asyncio
from enum import Enum
import struct
import logging
import socket
import time
import asyncio
import numpy as np
from sipyco import keepalive


logger = logging.getLogger(__name__)


class MessageType(Enum):
    output = 0b00
    input = 0b01
    exception = 0b10
    stopped = 0b11


class ExceptionType(Enum):
    legacy_reset = 0b000000
    legacy_reset_falling = 0b000001
    legacy_reset_phy = 0b000010
    legacy_reset_phy_falling = 0b000011
    legacy_o_underflow_reset = 0b010000
    legacy_o_sequence_error_reset = 0b010001
    legacy_o_collision_reset = 0b010010
    legacy_i_overflow_reset = 0b100000
    legacy_o_sequence_error = 0b010101

    o_underflow = 0b010100

    i_overflow = 0b100001


def get_analyzer_dump(host, port=1382):
    sock = socket.create_connection((host, port))
    try:
        r = bytes()
        while True:
            buf = sock.recv(8192)
            if not buf:
                break
            r += buf
    finally:
        sock.close()
    return r


OutputMessage = namedtuple(
    "OutputMessage", "channel timestamp rtio_counter address data")

InputMessage = namedtuple(
    "InputMessage", "channel timestamp rtio_counter data")

ExceptionMessage = namedtuple(
    "ExceptionMessage", "channel rtio_counter exception_type")

StoppedMessage = namedtuple(
    "StoppedMessage", "rtio_counter")


async def create_work_limiter(work=100, pause=5):
    start = time.monotonic_ns()
    while True:
        yield
        if time.monotonic_ns() >= start + work * 1e6:
            await asyncio.sleep(0.001 * pause)
            start = time.monotonic_ns()


def decode_message(data):
    message_type_channel = struct.unpack(">I", data[28:32])[0]
    message_type = MessageType(message_type_channel & 0b11)
    channel = message_type_channel >> 2

    if message_type == MessageType.output:
        parts = struct.unpack(">QIQQ", data[:28])
        data, address, rtio_counter, timestamp = parts
        return OutputMessage(channel, timestamp, rtio_counter, address, data)
    elif message_type == MessageType.input:
        parts = struct.unpack(">QIQQ", data[:28])
        data, _, rtio_counter, timestamp = parts
        return InputMessage(channel, timestamp, rtio_counter, data)
    elif message_type == MessageType.exception:
        exception_type, rtio_counter = struct.unpack(">BQ", data[11:20])
        return ExceptionMessage(channel, rtio_counter,
                                ExceptionType(exception_type))
    elif message_type == MessageType.stopped:
        rtio_counter = struct.unpack(">Q", data[12:20])[0]
        return StoppedMessage(rtio_counter)
    else:
        raise ValueError


DecodedDump = namedtuple(
    "DecodedDump", "log_channel dds_onehot_sel messages")


# simplified from sipyco broadcast Receiver
class AnalyzerProxyReceiver:
    def __init__(self, receive_cb):
        self.receive_cb = receive_cb

    async def connect(self, host, port):
        self.reader, self.writer = \
            await keepalive.async_open_connection(host, port)
        try:
            self.receive_task = asyncio.ensure_future(self._receive_cr())
        except:
            self.writer.close()
            del self.reader
            del self.writer
            raise

    async def close(self):
        try:
            self.receive_task.cancel()
            try:
                await asyncio.wait_for(self.receive_task, None)
            except asyncio.CancelledError:
                pass
        finally:
            self.writer.close()
            del self.reader
            del self.writer

    async def _receive_cr(self):
        try:
            while True:
                endian_byte = await self.reader.readexactly(1)
                if endian_byte == b"E":
                    endian = '>'
                elif endian_byte == b"e":
                    endian = '<'
                else:
                    raise ValueError
                payload_length_word = await self.reader.readexactly(4)
                payload_length = struct.unpack(endian + "I", payload_length_word)[0]
                if payload_length > 10 * 512 * 1024:
                    # 10x buffer size of firmware
                    raise ValueError
                data = await self.reader.readexactly(payload_length + 11)
                self.receive_cb(endian, payload_length, data)
        finally:
            pass


def _decode_dump_prelude(data):
    # extract endian byte
    if data[0] == ord('E'):
        endian = '>'
    elif data[0] == ord('e'):
        endian = '<'
    else:
        raise ValueError
    data = data[1:]
    # only header is device endian
    # messages are big endian
    parts = struct.unpack(endian + "IQbbb", data[:15])
    (sent_bytes, total_byte_count,
     error_occurred, log_channel, dds_onehot_sel) = parts

    expected_len = sent_bytes + 15
    if expected_len != len(data):
        raise ValueError("analyzer dump has incorrect length "
                         "(got {}, expected {})".format(
                            len(data), expected_len))
    if error_occurred:
        logger.warning("error occurred within the analyzer, "
                       "data may be corrupted")
    if total_byte_count > sent_bytes:
        logger.info("analyzer ring buffer has wrapped %d times",
                    total_byte_count//sent_bytes)
    return sent_bytes, data, log_channel, dds_onehot_sel


def decode_dump(data):
    sent_bytes, data, log_channel, dds_onehot_sel = _decode_dump_prelude(data)
    position = 15
    messages = []
    for _ in range(sent_bytes//32):
        messages.append(decode_message(data[position:position+32]))
        position += 32
    return DecodedDump(log_channel, bool(dds_onehot_sel), messages)


def decode_header_from_receiver(endian, payload_length, data):
    parts = struct.unpack(endian + "Qbbb", data[:11])
    (total_byte_count,
     error_occurred, log_channel, dds_onehot_sel) = parts

    expected_len = payload_length + 11
    if expected_len != len(data):
        raise ValueError("analyzer dump has incorrect length",
                          "({}/{})".format(expected_len, len(data)))
    if error_occurred:
        logger.warning("error occurred within the analyzer, "
                       "data may be corrupted")
    if total_byte_count > payload_length:
        logger.info("analyzer ring buffer has wrapped %d times",
                    total_byte_count // payload_length)
    data = data[11:]
    return (payload_length, log_channel, dds_onehot_sel, data)


def decode_dump_loop(payload_length, log_channel, dds_onehot_sel, data):
    messages = []
    position = 0
    for _ in range(payload_length // 32):
        messages.append(decode_message(data[position:position + 32]))
        position += 32
    return DecodedDump(log_channel, bool(dds_onehot_sel), messages)


# slower decoder with delay to allow gui updates
async def async_decode_dump(data, work_period=95, wait_period=5):
    sent_bytes, data, log_channel, dds_onehot_sel = _decode_dump_prelude(data)
    work_limiter = create_work_limiter(work_period, wait_period)
    position = 15
    messages = []
    for _ in range(sent_bytes//32):
        await anext(work_limiter)
        messages.append(decode_message(data[position:position+32]))
        position += 32
    return DecodedDump(log_channel, bool(dds_onehot_sel), messages)

# simplified from sipyco broadcast Receiver
class AnalyzerProxyReceiver:
    def __init__(self, receive_cb):
        self.receive_cb = receive_cb

    async def connect(self, host, port):
        self.reader, self.writer = \
            await keepalive.async_open_connection(host, port)
        try:
            self.receive_task = asyncio.ensure_future(self._receive_cr())
        except:
            self.writer.close()
            del self.reader
            del self.writer
            raise

    async def close(self):
        try:
            self.receive_task.cancel()
            try:
                await asyncio.wait_for(self.receive_task, None)
            except asyncio.CancelledError:
                pass
        finally:
            self.writer.close()
            del self.reader
            del self.writer

    async def _receive_cr(self):
        try:
            while True:
                endian_byte = await self.reader.readexactly(1)
                if endian_byte == b"E":
                    endian = '>'
                elif endian_byte == b"e":
                    endian = '<'
                else:
                    raise ValueError
                payload_length_word = await self.reader.readexactly(4)
                payload_length = struct.unpack(endian + "I", payload_length_word)[0]
                if payload_length > 10 * 512 * 1024:
                    # 10x buffer size of firmware
                    raise ValueError
                data = await self.reader.readexactly(payload_length + 11)
                self.receive_cb(endian, payload_length, data)
        finally:
            pass


def vcd_codes():
    codechars = [chr(i) for i in range(33, 127)]
    for n in count():
        q, r = divmod(n, len(codechars))
        code = codechars[r]
        while q > 0:
            q, r = divmod(q, len(codechars))
            code = codechars[r] + code
        yield code


class VCDChannel:
    def __init__(self, out, code):
        self.out = out
        self.code = code

    def set_value(self, value):
        if len(value) > 1:
            self.out.write("b" + value + " " + self.code + "\n")
        else:
            self.out.write(value + self.code + "\n")

    def set_value_double(self, x):
        integer_cast = struct.unpack(">Q", struct.pack(">d", x))[0]
        self.set_value("{:064b}".format(integer_cast))

    def set_log(self, log_message):
        value = ""
        for c in log_message:
            value += "{:08b}".format(ord(c))
        self.set_value(value)


class VCDManager:
    def __init__(self, fileobj):
        self.out = fileobj
        self.codes = vcd_codes()
        self.current_time = None

    def set_timescale_ps(self, timescale):
        self.out.write("$timescale {}ps $end\n".format(round(timescale)))

    def get_channel(self, name, width):
        code = next(self.codes)
        self.out.write("$var wire {width} {code} {name} $end\n"
                       .format(name=name, code=code, width=width))
        return VCDChannel(self.out, code)

    @contextmanager
    def scope(self, scope, name):
        self.out.write("$scope module {}/{} $end\n".format(scope, name))
        yield
        self.out.write("$upscope $end\n")

    def set_time(self, time):
        if time != self.current_time:
            self.out.write("#{}\n".format(time))
            self.current_time = time

    def set_end_time(self, time):
        pass


class WaveformChannel:
    def __init__(self, data, current_time):
        self.data = data
        self.current_time = current_time

    def set_value(self, value):
        if value == "X":
            value = None
        else:
            value = int(value, 2)
        self.data.append((self.current_time, value))

    def set_value_double(self, x):
        self.data.append((self.current_time, x))

    def set_time(self, time):
        self.current_time = time

    def set_log(self, message):
        self.data.append((self.current_time, message))


class WaveformManager:

    def __init__(self):
        self.current_time = 0
        self.channels = list()
        self.current_scope = ""
        self.trace = {"timescale": None, "stopped_x": None, "logs": dict(), "data": dict()}

    def set_timescale_ps(self, timescale):
        self.trace["timescale"] = timescale

    def get_channel(self, name, width, ty):
        if ty == "log":
            data = self.trace["logs"][self.current_scope + name] = list()
        else:
            data = self.trace["data"][self.current_scope + name] = list()
        channel = WaveformChannel(data, self.current_time)
        self.channels.append(channel)
        return channel

    @contextmanager
    def scope(self, scope, name):
        old_scope = self.current_scope
        self.current_scope = scope + "/"
        yield
        self.current_scope = old_scope

    def set_time(self, time):
        for channel in self.channels:
            channel.set_time(time * 1e-12 * self.trace["timescale"])

    def set_end_time(self, time):
        self.trace["stopped_x"] = time * 1e-12 * self.trace["timescale"]


class ChannelsOnlyManager:

    def __init__(self):
        self.current_scope = ""
        self.channels = dict()

    def get_channel(self, name, width, ty):
        self.channels[self.current_scope + name] = (width, ty)
        return None

    @contextmanager
    def scope(self, scope, name):
        old_scope = self.current_scope
        self.current_scope = scope + "/"
        yield
        self.current_scope = old_scope


class TTLHandler:
    def __init__(self, manager, name):
        self.name = name
        self.channel_value = manager.get_channel("ttl/" + name, 1, ty="bit")
        self.last_value = "X"
        self.oe = True

    def process_message(self, message):
        if isinstance(message, OutputMessage):
            logger.debug("TTL write @%d %d to %d, name: %s",
                message.timestamp, message.data, message.address, self.name)
            if message.address == 0:
                self.last_value = str(message.data)
                if self.oe:
                    self.channel_value.set_value(self.last_value)
            elif message.address == 1:
                self.oe = bool(message.data)
                if self.oe:
                    self.channel_value.set_value(self.last_value)
                else:
                    self.channel_value.set_value("X")
        elif isinstance(message, InputMessage):
            logger.debug("TTL read  @%d %d, name: %s",
                message.timestamp, message.data, self.name)
            self.channel_value.set_value(str(message.data))


class TTLClockGenHandler:
    def __init__(self, manager, name, ref_period):
        self.name = name
        self.ref_period = ref_period
        self.channel_frequency = manager.get_channel(
            "ttl_clkgen/" + name, 64, ty="analog")

    def process_message(self, message):
        if isinstance(message, OutputMessage):
            logger.debug("TTL_CLKGEN write @%d %d to %d, name: %s",
                message.timestamp, message.data, message.address, self.name)
            frequency = message.data/self.ref_period/2**24
            self.channel_frequency.set_value_double(frequency)


class DDSHandler:
    def __init__(self, manager, onehot_sel, sysclk):
        self.manager = manager
        self.onehot_sel = onehot_sel
        self.sysclk = sysclk

        self.selected_dds_channels = set()
        self.dds_channels = dict()

    def add_dds_channel(self, name, dds_channel_nr):
        dds_channel = dict()
        with self.manager.scope("dds", name):
            dds_channel["vcd_frequency"] = \
                self.manager.get_channel(name + "/frequency", 64, ty="analog")
            dds_channel["vcd_phase"] = \
                self.manager.get_channel(name + "/phase", 64, ty="analog")
        dds_channel["ftw"] = [None, None]
        dds_channel["pow"] = None
        self.dds_channels[dds_channel_nr] = dds_channel

    def _gpio_to_channels(self, gpio):
        gpio >>= 1  # strip reset
        if self.onehot_sel:
            r = set()
            nr = 0
            mask = 1
            while gpio >= mask:
                if gpio & mask:
                    r.add(nr)
                nr += 1
                mask *= 2
            return r
        else:
            return {gpio}

    def _decode_ad9914_write(self, message):
        if message.address == 0x81:
            self.selected_dds_channels = self._gpio_to_channels(message.data)
        for dds_channel_nr in self.selected_dds_channels:
            dds_channel = self.dds_channels[dds_channel_nr]
            if message.address == 0x11:
                dds_channel["ftw"][0] = message.data
            elif message.address == 0x13:
                dds_channel["ftw"][1] = message.data
            elif message.address == 0x31:
                dds_channel["pow"] = message.data
            elif message.address == 0x80:  # FUD
                if None not in dds_channel["ftw"]:
                    ftw = sum(x << i*16
                              for i, x in enumerate(dds_channel["ftw"]))
                    frequency = ftw*self.sysclk/2**32
                    dds_channel["vcd_frequency"].set_value_double(frequency)
                if dds_channel["pow"] is not None:
                    phase = dds_channel["pow"]/2**16
                    dds_channel["vcd_phase"].set_value_double(phase)

    def process_message(self, message):
        if isinstance(message, OutputMessage):
            logger.debug("DDS write @%d 0x%04x to 0x%02x, selected channels: %s",
                         message.timestamp, message.data, message.address,
                         self.selected_dds_channels)
            self._decode_ad9914_write(message)


class WishboneHandler:
    def __init__(self, manager, name, read_bit):
        self._reads = []
        self._read_bit = read_bit
        self.stb = manager.get_channel(name + "/stb", 1, ty="bit")

    def process_message(self, message):
        self.stb.set_value("1")
        self.stb.set_value("0")
        if isinstance(message, OutputMessage):
            logger.debug("Wishbone out @%d adr=0x%02x data=0x%08x",
                         message.timestamp, message.address, message.data)
            if message.address & self._read_bit:
                read = self._reads.pop(0)
                self.process_read(
                        message.address & ~self._read_bit,
                        read.data,
                        read.rtio_counter - message.timestamp)
            else:
                self.process_write(message.address,
                        message.data)
        if isinstance(message, InputMessage):
            logger.debug("Wishbone in @%d data=0x%08x",
                         message.rtio_counter, message.data)
            self._reads.append(message)

    def process_write(self, address, data):
        raise NotImplementedError

    def process_read(self, address, data, read_slack):
        raise NotImplementedError


class SPIMasterHandler(WishboneHandler):
    def __init__(self, manager, name):
        self.channels = {}
        self.scope = "spi"
        with manager.scope("spi", name):
            super().__init__(manager, name, read_bit=0b100)
            for reg_name, reg_width in [
                    ("config", 32), ("chip_select", 16),
                    ("write_length", 8), ("read_length", 8),
                    ("write", 32), ("read", 32)]:
                self.channels[reg_name] = manager.get_channel(
                    "{}/{}".format(name, reg_name), reg_width, ty="vector")

    def process_write(self, address, data):
        if address == 0:
            self.channels["write"].set_value("{:032b}".format(data))
        elif address == 1:
            self.channels["chip_select"].set_value(
                    "{:08b}".format(data & 0xffff))
            self.channels["write_length"].set_value(
                    "{:08b}".format(data >> 16 & 0xff))
            self.channels["read_length"].set_value(
                    "{:08b}".format(data >> 24 & 0xff))
        elif address == 2:
            self.channels["config"].set_value("{:032b}".format(data))
        else:
            raise ValueError("bad address %d", address)

    def process_read(self, address, data, read_slack):
        if address == 0:
            self.channels["read"].set_value("{:032b}".format(data))
        else:
            raise ValueError("bad address %d", address)


class SPIMaster2Handler(WishboneHandler):
    def __init__(self, manager, name):
        self._reads = []
        self.channels = {}
        self.scope = "spi2"
        with manager.scope("spi2", name):
            self.stb = manager.get_channel(name + "/stb", 1, ty="ttl")
            for reg_name, reg_width in [
                    ("flags", 8),
                    ("length", 5),
                    ("div", 8),
                    ("chip_select", 8),
                    ("write", 32),
                    ("read", 32)]:
                self.channels[reg_name] = manager.get_channel(
                    "{}/{}".format(name, reg_name), reg_width, ty="vector")

    def process_message(self, message):
        self.stb.set_value("1")
        self.stb.set_value("0")
        if isinstance(message, OutputMessage):
            data = message.data
            address = message.address
            if address == 1:
                logger.debug("SPI config @%d data=0x%08x",
                         message.timestamp, data)
                self.channels["chip_select"].set_value(
                        "{:08b}".format(data >> 24))
                self.channels["div"].set_value(
                        "{:08b}".format(data >> 16 & 0xff))
                self.channels["length"].set_value(
                        "{:08b}".format(data >> 8 & 0x1f))
                self.channels["flags"].set_value(
                        "{:08b}".format(data & 0xff))
            elif address == 0:
                logger.debug("SPI write @%d data=0x%08x",
                         message.timestamp, data)
                self.channels["write"].set_value("{:032b}".format(data))
            else:
                raise ValueError("bad address", address)
            # process untimed reads and insert them here
            while (self._reads and
                   self._reads[0].rtio_counter < message.timestamp):
                read = self._reads.pop(0)
                logger.debug("SPI read @%d data=0x%08x",
                            read.rtio_counter, read.data)
                self.channels["read"].set_value("{:032b}".format(read.data))
        elif isinstance(message, InputMessage):
            self._reads.append(message)


def _extract_log_chars(data):
    r = ""
    for i in range(4):
        n = data >> 24
        data = (data << 8) & 0xffffffff
        if not n:
            continue
        r += chr(n)
    return r


class LogHandler:
    def __init__(self, manager, log_channels):
        self.channels = dict()
        for name, maxlength in log_channels.items():
            self.channels[name] = manager.get_channel("logs/" + name,
                                                      maxlength * 8,
                                                      ty="log")
        self.current_entry = ""

    def process_message(self, message):
        if isinstance(message, OutputMessage):
            self.current_entry += _extract_log_chars(message.data)
            if len(self.current_entry) > 1 and self.current_entry[-1] == "\x1D":
                channel_name, log_message = self.current_entry[:-1].split("\x1E", maxsplit=1)
                self.channels[channel_name].set_log(log_message)
                self.current_entry = ""


def get_log_channels(log_channel, messages):
    log_channels = dict()
    log_entry = ""
    for message in messages:
        if (isinstance(message, OutputMessage)
                and message.channel == log_channel):
            log_entry += _extract_log_chars(message.data)
            if len(log_entry) > 1 and log_entry[-1] == "\x1D":
                channel_name, log_message = log_entry[:-1].split("\x1E", maxsplit=1)
                l = len(log_message)
                if channel_name in log_channels:
                    if log_channels[channel_name] < l:
                        log_channels[channel_name] = l
                else:
                    log_channels[channel_name] = l
                log_entry = ""
    return log_channels


def get_single_device_argument(devices, module, cls, argument):
    found = None
    for desc in devices.values():
        if isinstance(desc, dict) and desc["type"] == "local":
            if (desc["module"] == module
                    and desc["class"] in cls):
                value = desc["arguments"][argument]
                if found is None:
                    found = value
                elif value != found:
                    return None  # more than one value/device found
    return found


def get_ref_period(devices):
    return get_single_device_argument(devices, "artiq.coredevice.core",
                                      ("Core",), "ref_period")


def get_dds_sysclk(devices):
    return get_single_device_argument(devices, "artiq.coredevice.ad9914",
                                      ("AD9914",), "sysclk")


def create_channel_handlers(manager, devices, ref_period,
                            dds_sysclk, dds_onehot_sel):
    channel_handlers = dict()
    for name, desc in sorted(devices.items(), key=itemgetter(0)):
        if isinstance(desc, dict) and desc["type"] == "local":
            if (desc["module"] == "artiq.coredevice.ttl"
                    and desc["class"] in {"TTLOut", "TTLInOut"}):
                channel = desc["arguments"]["channel"]
                channel_handlers[channel] = TTLHandler(manager, name)
            if (desc["module"] == "artiq.coredevice.ttl"
                    and desc["class"] == "TTLClockGen"):
                channel = desc["arguments"]["channel"]
                channel_handlers[channel] = TTLClockGenHandler(manager, name, ref_period)
            if (desc["module"] == "artiq.coredevice.ad9914"
                    and desc["class"] == "AD9914"):
                dds_bus_channel = desc["arguments"]["bus_channel"]
                dds_channel = desc["arguments"]["channel"]
                if dds_bus_channel in channel_handlers:
                    dds_handler = channel_handlers[dds_bus_channel]
                else:
                    dds_handler = DDSHandler(manager, dds_onehot_sel, dds_sysclk)
                    channel_handlers[dds_bus_channel] = dds_handler
                dds_handler.add_dds_channel(name, dds_channel)
            if (desc["module"] == "artiq.coredevice.spi2" and
                    desc["class"] == "SPIMaster"):
                channel = desc["arguments"]["channel"]
                channel_handlers[channel] = SPIMaster2Handler(
                        manager, name)
    return channel_handlers


def get_message_time(message):
    return getattr(message, "timestamp", message.rtio_counter)


def decoded_dump_to_vcd(fileobj, devices, dump, uniform_interval=False):
    vcd_manager = VCDManager(fileobj)
    _decoded_dump_to_target(vcd_manager, devices, dump, uniform_interval)


def decoded_dump_to_waveform(devices, dump, uniform_interval=False):
    manager = WaveformManager()
    _decoded_dump_to_target(manager, devices, dump, uniform_interval)
    return manager.trace


async def async_decoded_dump_to_vcd(fileobj, devices, dump, uniform_interval=False,
                                    work_period=95, wait_period=5):
    vcd_manager = VCDManager(fileobj)
    await _async_decoded_dump_to_target(vcd_manager, devices, dump, uniform_interval,
                                        work_period, wait_period)


async def async_decoded_dump_to_waveform(devices, dump, uniform_interval=False,
                                         work_period=95, wait_period=5):
    manager = WaveformManager()
    await _async_decoded_dump_to_target(manager, devices, dump, uniform_interval,
                                        work_period, wait_period)
    return manager.trace


def get_channel_list(devices):
    manager = ChannelsOnlyManager()
    create_channel_handlers(manager, devices, 1e-9, 3e9, False)
    manager.get_channel("timestamp", 64, ty="vector")
    manager.get_channel("interval", 64, ty="analog")
    manager.get_channel("rtio_slack", 64, ty="analog")
    return manager.channels


PreludeArgs = namedtuple("PreludeArgs",
                         "manager channel_handlers messages start_time \
                         ref_period slack interval timestamp")


def _decoded_dump_to_target_prelude(manager, devices, dump, uniform_interval):
    ref_period = get_ref_period(devices)

    if ref_period is not None:
        if not uniform_interval:
            manager.set_timescale_ps(ref_period*1e12)
    else:
        logger.warning("unable to determine core device ref_period")
        ref_period = 1e-9  # guess
    dds_sysclk = get_dds_sysclk(devices)
    if dds_sysclk is None:
        logger.warning("unable to determine DDS sysclk")
        dds_sysclk = 3e9  # guess

    if isinstance(dump.messages[-1], StoppedMessage):
        m = dump.messages[-1]
        end_time = get_message_time(m)
        messages = dump.messages[:-1]
    else:
        logger.warning("StoppedMessage missing")
        messages = dump.messages
    messages = sorted(messages, key=get_message_time)

    channel_handlers = create_channel_handlers(
        manager, devices, ref_period,
        dds_sysclk, dump.dds_onehot_sel)
    log_channels = get_log_channels(dump.log_channel, messages)
    channel_handlers[dump.log_channel] = LogHandler(
        manager, log_channels)
    interval = None
    timestamp = None
    if uniform_interval:
        # RTIO event timestamp in machine units
        timestamp = manager.get_channel("timestamp", 64, ty="analog")
        # RTIO time interval between this and the next timed event
        # in SI seconds
        interval = manager.get_channel("interval", 64, ty="analog")
    slack = manager.get_channel("rtio_slack", 64, ty="analog")

    manager.set_time(0)
    start_time = 0
    for m in messages:
        start_time = get_message_time(m)
        if start_time:
            break

    if end_time:
        manager.set_end_time(end_time - start_time)

    return PreludeArgs(manager, channel_handlers, messages,
                       start_time, ref_period, slack, interval, timestamp)


def _decoded_dump_to_target(manager, devices, dump, uniform_interval):
    args = _decoded_dump_to_target_prelude(manager, devices, dump, uniform_interval)
    t0 = 0
    for i, message in enumerate(args.messages):
        if message.channel in args.channel_handlers:
            t = get_message_time(message) - args.start_time
            if t >= 0:
                if uniform_interval:
                    args.interval.set_value_double((t - t0)*args.ref_period)
                    args.manager.set_time(i)
                    args.timestamp.set_value("{:064b}".format(t))
                    t0 = t
                else:
                    args.manager.set_time(t)
            args.channel_handlers[message.channel].process_message(message)
            if isinstance(message, OutputMessage):
                args.slack.set_value_double(
                    (message.timestamp - message.rtio_counter)*args.ref_period)


# rate limited to prevent event loop lag, total conversion time will be longer...
async def _async_decoded_dump_to_target(manager, devices, dump, uniform_interval,
                                        work_period, wait_period):
    args = _decoded_dump_to_target_prelude(manager, devices, dump, uniform_interval)
    work_limiter = create_work_limiter(work_period, wait_period)
    t0 = 0
    for i, message in enumerate(args.messages):
        await anext(work_limiter)
        if message.channel in args.channel_handlers:
            t = get_message_time(message) - args.start_time
            if t >= 0:
                if uniform_interval:
                    args.interval.set_value_double((t - t0)*args.ref_period)
                    args.manager.set_time(i)
                    args.timestamp.set_value("{:064b}".format(t))
                    t0 = t
                else:
                    args.manager.set_time(t)
            args.channel_handlers[message.channel].process_message(message)
            if isinstance(message, OutputMessage):
                args.slack.set_value_double(
                    (message.timestamp - message.rtio_counter)*args.ref_period)
