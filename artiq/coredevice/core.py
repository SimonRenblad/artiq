import os, sys
import numpy
from inspect import getfullargspec
from functools import wraps

from pythonparser import diagnostic

from artiq import __artiq_dir__ as artiq_dir

from artiq.language.core import *
from artiq.language.types import *
from artiq.language.units import *

from artiq.compiler.module import Module
from artiq.compiler.embedding import Stitcher
from artiq.compiler.targets import RV32IMATarget, RV32GTarget, CortexA9Target

from artiq.coredevice.comm_kernel import CommKernel, CommKernelDummy
# Import for side effects (creating the exception classes).
from artiq.coredevice import exceptions


def _render_diagnostic(diagnostic, colored):
    def shorten_path(path):
        return path.replace(artiq_dir, "<artiq>")
    lines = [shorten_path(path) for path in diagnostic.render(colored=colored)]
    return "\n".join(lines)

colors_supported = os.name == "posix"
class _DiagnosticEngine(diagnostic.Engine):
    def render_diagnostic(self, diagnostic):
        sys.stderr.write(_render_diagnostic(diagnostic, colored=colors_supported) + "\n")

class CompileError(Exception):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic

    def __str__(self):
        # Prepend a newline so that the message shows up on after
        # exception class name printed by Python.
        return "\n" + _render_diagnostic(self.diagnostic, colored=colors_supported)


@syscall
def rtio_init() -> TNone:
    raise NotImplementedError("syscall not simulated")

@syscall(flags={"nounwind", "nowrite"})
def rtio_get_destination_status(linkno: TInt32) -> TBool:
    raise NotImplementedError("syscall not simulated")

@syscall(flags={"nounwind", "nowrite"})
def rtio_get_counter() -> TInt64:
    raise NotImplementedError("syscall not simulated")


def get_target_cls(target):
    if target == "rv32g":
        return RV32GTarget
    elif target == "rv32ima":
        return RV32IMATarget
    elif target == "cortexa9":
        return CortexA9Target
    else:
        raise ValueError("Unsupported target")


class Core:
    """Core device driver.

    :param host: hostname or IP address of the core device.
    :param ref_period: period of the reference clock for the RTIO subsystem.
        On platforms that use clock multiplication and SERDES-based PHYs,
        this is the period after multiplication. For example, with a RTIO core
        clocked at 125MHz and a SERDES multiplication factor of 8, the
        reference period is 1ns.
        The time machine unit is equal to this period.
    :param ref_multiplier: ratio between the RTIO fine timestamp frequency
        and the RTIO coarse timestamp frequency (e.g. SERDES multiplication
        factor).
    :param analyzer_proxy: name of the core device analyzer proxy to trigger
        (optional).
    :param analyze_at_run_end: automatically trigger the core device analyzer
        proxy after the Experiment's run stage finishes.
    """

    kernel_invariants = {
        "core", "ref_period", "coarse_ref_period", "ref_multiplier",
    }

    def __init__(self, dmgr,
                 host, ref_period,
                 analyzer_proxy=None, analyze_at_run_end=False,
                 ref_multiplier=8,
                 target="rv32g", satellite_cpu_targets={}):
        self.ref_period = ref_period
        self.ref_multiplier = ref_multiplier
        self.satellite_cpu_targets = satellite_cpu_targets
        self.target_cls = get_target_cls(target)
        self.coarse_ref_period = ref_period*ref_multiplier
        if host is None:
            self.comm = CommKernelDummy()
        else:
            self.comm = CommKernel(host)
        self.analyzer_proxy_name = analyzer_proxy
        self.analyze_at_run_end = analyze_at_run_end

        self.first_run = True
        self.dmgr = dmgr
        self.core = self
        self.comm.core = self
        self.analyzer_proxy = None

    def notify_run_end(self):
        if self.analyze_at_run_end:
            self.trigger_analyzer_proxy()

    def close(self):
        self.comm.close()

    def compile(self, function, args, kwargs, set_result=None,
                attribute_writeback=True, print_as_rpc=True,
                target=None, destination=0, subkernel_arg_types=[],
                subkernels={}):
        try:
            engine = _DiagnosticEngine(all_errors_are_fatal=True)

            stitcher = Stitcher(engine=engine, core=self, dmgr=self.dmgr,
                                print_as_rpc=print_as_rpc,
                                destination=destination, subkernel_arg_types=subkernel_arg_types,
                                subkernels=subkernels)
            stitcher.stitch_call(function, args, kwargs, set_result)
            stitcher.finalize()

            module = Module(stitcher,
                ref_period=self.ref_period,
                attribute_writeback=attribute_writeback)
            target = target if target is not None else self.target_cls()

            library = target.compile_and_link([module])
            stripped_library = target.strip(library)

            return stitcher.embedding_map, stripped_library, \
                   lambda addresses: target.symbolize(library, addresses), \
                   lambda symbols: target.demangle(symbols), \
                   module.subkernel_arg_types
        except diagnostic.Error as error:
            raise CompileError(error.diagnostic) from error

    def _run_compiled(self, kernel_library, embedding_map, symbolizer, demangler):
        if self.first_run:
            self.comm.check_system_info()
            self.first_run = False
        self.comm.load(kernel_library)
        self.comm.run()
        self.comm.serve(embedding_map, symbolizer, demangler)

    def run(self, function, args, kwargs):
        result = None
        @rpc(flags={"async"})
        def set_result(new_result):
            nonlocal result
            result = new_result
        embedding_map, kernel_library, symbolizer, demangler, subkernel_arg_types = \
            self.compile(function, args, kwargs, set_result)
        self.compile_and_upload_subkernels(embedding_map, args, subkernel_arg_types)
        self._run_compiled(kernel_library, embedding_map, symbolizer, demangler)
        return result

    def compile_subkernel(self, sid, subkernel_fn, embedding_map, args, subkernel_arg_types, subkernels):
        # pass self to subkernels (if applicable)
        # assuming the first argument is self
        subkernel_args = getfullargspec(subkernel_fn.artiq_embedded.function)
        self_arg = []
        if len(subkernel_args[0]) > 0:
            if subkernel_args[0][0] == 'self':
                self_arg = args[:1]
        destination = subkernel_fn.artiq_embedded.destination
        destination_tgt = self.satellite_cpu_targets[destination]
        target = get_target_cls(destination_tgt)(subkernel_id=sid)
        object_map, kernel_library, _, _, _ = \
            self.compile(subkernel_fn, self_arg, {}, attribute_writeback=False,
                        print_as_rpc=False, target=target, destination=destination, 
                        subkernel_arg_types=subkernel_arg_types.get(sid, []),
                        subkernels=subkernels)
        if object_map.has_rpc():
            raise ValueError("Subkernel must not use RPC")
        return destination, kernel_library, object_map

    def compile_and_upload_subkernels(self, embedding_map, args, subkernel_arg_types):
        subkernels = embedding_map.subkernels()
        subkernels_compiled = []
        while True:
            new_subkernels = {}
            for sid, subkernel_fn in subkernels.items():
                if sid in subkernels_compiled:
                    continue
                destination, kernel_library, sub_embedding_map = \
                    self.compile_subkernel(sid, subkernel_fn, embedding_map,
                                        args, subkernel_arg_types, subkernels)
                self.comm.upload_subkernel(kernel_library, sid, destination)
                new_subkernels.update(sub_embedding_map.subkernels())
                subkernels_compiled.append(sid)
            if new_subkernels == subkernels:
                break
            subkernels.update(new_subkernels)


    def precompile(self, function, *args, **kwargs):
        """Precompile a kernel and return a callable that executes it on the core device
        at a later time.

        Arguments to the kernel are set at compilation time and passed to this function,
        as additional positional and keyword arguments.
        The returned callable accepts no arguments.

        Precompiled kernels may use RPCs and subkernels.

        Object attributes at the beginning of a precompiled kernel execution have the
        values they had at precompilation time. If up-to-date values are required,
        use RPC to read them.
        Similarly, modified values are not written back, and explicit RPC should be used
        to modify host objects.
        Carefully review the source code of drivers calls used in precompiled kernels, as
        they may rely on host object attributes being transfered between kernel calls.
        Examples include code used to control DDS phase, and Urukul RF switch control
        via the CPLD register.

        The return value of the callable is the return value of the kernel, if any.

        The callable may be called several times.
        """
        if not hasattr(function, "artiq_embedded"):
            raise ValueError("Argument is not a kernel")

        result = None
        @rpc(flags={"async"})
        def set_result(new_result):
            nonlocal result
            result = new_result

        embedding_map, kernel_library, symbolizer, demangler, subkernel_arg_types = \
            self.compile(function, args, kwargs, set_result, attribute_writeback=False)
        self.compile_and_upload_subkernels(embedding_map, args, subkernel_arg_types)

        @wraps(function)
        def run_precompiled():
            nonlocal result
            self._run_compiled(kernel_library, embedding_map, symbolizer, demangler)
            return result

        return run_precompiled

    @portable
    def seconds_to_mu(self, seconds):
        """Convert seconds to the corresponding number of machine units
        (RTIO cycles).

        :param seconds: time (in seconds) to convert.
        """
        return numpy.int64(seconds//self.ref_period)

    @portable
    def mu_to_seconds(self, mu):
        """Convert machine units (RTIO cycles) to seconds.

        :param mu: cycle count to convert.
        """
        return mu*self.ref_period

    @kernel
    def get_rtio_counter_mu(self):
        """Retrieve the current value of the hardware RTIO timeline counter.

        As the timing of kernel code executed on the CPU is inherently
        non-deterministic, the return value is by necessity only a lower bound
        for the actual value of the hardware register at the instant when
        execution resumes in the caller.

        For a more detailed description of these concepts, see :doc:`/rtio`.
        """
        return rtio_get_counter()

    @kernel
    def wait_until_mu(self, cursor_mu):
        """Block execution until the hardware RTIO counter reaches the given
        value (see :meth:`get_rtio_counter_mu`).

        If the hardware counter has already passed the given time, the function
        returns immediately.
        """
        while self.get_rtio_counter_mu() < cursor_mu:
            pass

    @kernel
    def get_rtio_destination_status(self, destination):
        """Returns whether the specified RTIO destination is up.
        This is particularly useful in startup kernels to delay
        startup until certain DRTIO destinations are up."""
        return rtio_get_destination_status(destination)

    @kernel
    def reset(self):
        """Clear RTIO FIFOs, release RTIO PHY reset, and set the time cursor
        at the current value of the hardware RTIO counter plus a margin of
        125000 machine units."""
        rtio_init()
        at_mu(rtio_get_counter() + 125000)

    @kernel
    def break_realtime(self):
        """Set the time cursor after the current value of the hardware RTIO
        counter plus a margin of 125000 machine units.

        If the time cursor is already after that position, this function
        does nothing."""
        min_now = rtio_get_counter() + 125000
        if now_mu() < min_now:
            at_mu(min_now)

    def trigger_analyzer_proxy(self):
        """Causes the core analyzer proxy to retrieve a dump from the device,
        and distribute it to all connected clients (typically dashboards).

        Returns only after the dump has been retrieved from the device.

        Raises IOError if no analyzer proxy has been configured, or if the
        analyzer proxy fails. In the latter case, more details would be
        available in the proxy log.
        """
        if self.analyzer_proxy is None:
            if self.analyzer_proxy_name is not None:
                self.analyzer_proxy = self.dmgr.get(self.analyzer_proxy_name)
        if self.analyzer_proxy is None:
            raise IOError("No analyzer proxy configured")
        else:
            success = self.analyzer_proxy.trigger()
            if not success:
                raise IOError("Analyzer proxy reported failure")
