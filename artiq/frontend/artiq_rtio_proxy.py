import argparse
import asyncio
from sipyco.asyncio_tools import AsyncioServer, SignalHandler
from sipyco.pc_rpc import Server
from sipyco.sync_struct import Notifier, Publisher
from sipyco import common_args

from artiq.coredevice.comm_analyzer import get_analyzer_dump

import inspect

# Turn into proxy interface
class RTIOAnalyzerControl:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.data = dict()
        self.notifier = Notifier(self.data)

    # rpc to pull from device
    def pull_from_device(self):
        # dump = get_analyzer_dump(self.host, self.port)
        dump = b"Hello World"
        with open("dump2.bin", "rb") as f:
            dump = f.read()
        self.notifier["data"] = dump

def get_argparser():
    parser = argparse.ArgumentParser(
        description="ARTIQ RTIO analyzer proxy")
    common_args.verbosity_args(parser)
    common_args.simple_network_args(parser, [
        ("proxy", "proxying", 1382),
        ("control", "control", 1385)
    ])
    #parser.add_argument("--simulation", action="store_true",
    #                    help="Simulation - does not connect to device")
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
            rtio_analyzer_control = RTIOAnalyzerControl(args.core_addr, args.port_proxy)
            dump_publisher = Publisher({"rtio_trace": rtio_analyzer_control.notifier})  
            loop.run_until_complete(dump_publisher.start(bind_address, args.port_proxy))
            try:
                server = Server({"rtio_proxy_control": rtio_analyzer_control}, None, True)
                loop.run_until_complete(server.start(bind_address, args.port_control))
                try:
                    _, pending = loop.run_until_complete(asyncio.wait(
                        [loop.create_task(signal_handler.wait_terminate()),
                         loop.create_task(server.wait_terminate())],
                        return_when=asyncio.FIRST_COMPLETED))
                    for task in pending:
                        task.cancel()
                finally:
                    loop.run_until_complete(server.stop())
            finally:
                loop.run_until_completed(dump_publisher.stop())
        finally:
            signal_handler.teardown()
    finally:
        loop.close()

if __name__ == "__main__":
    main()
