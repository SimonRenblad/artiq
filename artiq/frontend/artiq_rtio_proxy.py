import argparse
import asyncio
from sipyco.asyncio_tools import AsyncioServer, SignalHandler
from sipyco.pc_rpc import Server
from sipyco import common_args

from artiq.coredevice.comm_analyzer import get_analyzer_dump

# 1382 is default port for core analyzer 
# 1385 is not a port taken by defaults -> recommend for this

class ProxyConnection:
    def __init__(self, reader, writer, host, port):
        self.reader = reader
        self.writer = writer
        self.host = host
        self.port = port

    # naive version read smth -> writes back synchronously
    async def handle(self):
        ty = await self.reader.read(1) # read 1 byte
        if ty == b"\x00": # get dump
            #dump = get_analyzer_dump(self.host, self.port) 
            dump = b"Hello World!\n"
            with open("dump6.bin", "rb") as f:
                dump = f.read()
            self.writer.write(dump)
            self.writer.write_eof()
            await self.writer.drain()
            self.writer.close()

# Proxy the core analyzer
class ProxyServer(AsyncioServer):
    def __init__(self, host, port=1382):
        AsyncioServer.__init__(self)
        self.host = host
        self.port = port

    async def _handle_connection_cr(self, reader, writer):
        line = await reader.readline()
        if line != b"ARTIQ rtio analyzer\n":
            logger.error("incorrect magic")
            return
        await ProxyConnection(reader, writer, self.host, self.port).handle()


class PingTarget:
    def ping(self):
        return True

def get_argparser():
    parser = argparse.ArgumentParser(
        description="ARTIQ RTIO analyzer proxy")
    common_args.verbosity_args(parser)
    common_args.simple_network_args(parser, [
        ("proxy", "proxying", 1382),
        ("control", "control", 1385)
    ])
    parser.add_argument("core_addr", metavar="CORE_ADDR",
                        help="hostname or IP address of the core device")
    return parser

def main():
    args = get_argparser().parse_args()
    common_args.init_logger_from_args(args)

    bind_address = common_args.bind_address_from_args(args)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        signal_handler = SignalHandler() # handles Ctrl-C and terminate signals
        signal_handler.setup()
        try:
            proxy_server = ProxyServer(args.core_addr)
            loop.run_until_complete(proxy_server.start(bind_address, args.port_proxy))
            try:
                server = Server({"rtio_analyzer_proxy": PingTarget()}, None, True)
                loop.run_until_complete(server.start(bind_address, args.port_control))
                try:
                    _, pending = loop.run_until_complete(asyncio.wait(
                        [loop.create_task(signal_handler.wait_terminate()),
                         loop.create_task(server.wait_terminate())], # EDIT << 
                        return_when=asyncio.FIRST_COMPLETED))
                    for task in pending:
                        task.cancel()
                finally:
                    loop.run_until_complete(server.stop())
            finally:
                loop.run_until_complete(proxy_server.stop())
        finally:
            signal_handler.teardown()
    finally:
        loop.close()

if __name__ == "__main__":
    main()
