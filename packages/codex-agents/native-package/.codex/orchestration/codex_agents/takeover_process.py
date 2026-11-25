"""Pin and stop only a proven pre-B4t project service, never a collector or child.

Linux uses a pidfd when available. Older kernels use the last-birth-check rule,
as does macOS with NOTE_EXIT. The numeric signal's remaining PID reuse gap is
the residual accepted by decision 323, not a claim of pidfd-equivalent safety.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from pathlib import Path
import select
import signal
import struct
import sys
import time

from .update_takeover import TakeoverError

MAX_SCAN_PROCESSES = 4096
SCAN_SECONDS = 5.0
FD_SCAN_BYTES = 4 * 1024 * 1024


def bounded_processes(processes):
    started = time.monotonic()
    for index, pid in enumerate(processes):
        if index >= MAX_SCAN_PROCESSES or time.monotonic() - started > SCAN_SECONDS:
            raise TakeoverError('predecessor_unproven')
        yield pid


def require_free_locks(paths):
    for path in paths:
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileNotFoundError:
            continue
        except OSError:
            raise TakeoverError('predecessor_lock_inherited', phase='service_stopped') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)


def _darwin_library():
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    library.proc_listpids.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p, ctypes.c_int]
    library.proc_listpids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_pidfdinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidfdinfo.restype = ctypes.c_int
    return library


def _darwin_argv(pid):
    from . import process_tree
    process_tree._darwin_raw(pid)  # bind sysctl lazily, never at import
    # KERN_PROCARGS2 rejects a buffer larger than this kernel's ARG_MAX on
    # supported macOS releases. Read that bound first, within our own ceiling.
    maximum, maximum_size = ctypes.c_int(), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    argument_limit = (ctypes.c_int * 2)(1, 8)  # CTL_KERN, KERN_ARGMAX (SDK)
    if (process_tree._SYSCTL[0](argument_limit, 2, ctypes.byref(maximum), ctypes.byref(maximum_size), None, 0) != 0
            or maximum_size.value != ctypes.sizeof(maximum) or not 0 < maximum.value <= FD_SCAN_BYTES):
        raise OSError('process argument limit unavailable')
    raw = ctypes.create_string_buffer(maximum.value)
    size = ctypes.c_size_t(len(raw))
    mib = (ctypes.c_int * 3)(1, process_tree.DARWIN_KERN_PROCARGS2, pid)
    if process_tree._SYSCTL[0](mib, 3, raw, ctypes.byref(size), None, 0) != 0:
        raise OSError('process arguments unavailable')
    value = raw.raw[:size.value]
    argc = struct.unpack_from('i', value)[0]
    end = value.index(b'\0', 4) + 1
    while end < len(value) and value[end] == 0:
        end += 1
    args = value[end:].split(b'\0')[:argc]
    if len(args) != argc or not args or any(not arg for arg in args):
        raise OSError('process arguments unavailable')
    return [os.fsdecode(arg) for arg in args]


class BirthPin:
    """Older Linux kernel fallback, used only when pidfds are unavailable."""
    def __init__(self, pid, birth):
        self.pid, self.birth = pid, birth

    def close(self):
        pass


class Kernel:
    def pin(self, pid):
        if sys.platform.startswith('linux'):
            if hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
                try:
                    return os.pidfd_open(pid)
                except OSError as error:
                    if error.errno != errno.ENOSYS:
                        raise
            return BirthPin(pid, self.identity(pid)['birth'])
        if sys.platform == 'darwin':
            queue = select.kqueue()
            try:
                queue.control([select.kevent(pid, filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE, fflags=select.KQ_NOTE_EXIT)], 0, 0)
                return queue
            except BaseException:
                queue.close()
                raise
        raise OSError('process pin unavailable')

    def identity(self, pid):
        from .process_tree import process_record
        record = process_record(pid)
        if record is None or record[3]:
            raise OSError('process identity unavailable')
        if sys.platform == 'darwin':
            buffer = ctypes.create_string_buffer(136)  # struct proc_bsdinfo, SDK ABI
            size = _darwin_library().proc_pidinfo(pid, 3, 0, buffer, len(buffer))
            if size != len(buffer) or struct.unpack_from('I', buffer, 12)[0] != pid:
                raise OSError('process credentials unavailable')
            uid = struct.unpack_from('I', buffer, 20)[0]
            argv = _darwin_argv(pid)
        else:
            root = Path('/proc') / str(pid)
            uid = root.stat().st_uid
            with (root / 'cmdline').open('rb') as stream:
                raw = stream.read(FD_SCAN_BYTES + 1)
            if len(raw) > FD_SCAN_BYTES:
                raise OSError('process arguments exceed bound')
            argv = [os.fsdecode(arg) for arg in raw.rstrip(b'\0').split(b'\0')]
        return dict(uid=uid, birth=record[2], argv=argv)

    def exited(self, pin):
        if isinstance(pin, int):
            return bool(select.select([pin], [], [], 0)[0])
        if isinstance(pin, BirthPin):
            try:
                return self.identity(pin.pid)['birth'] != pin.birth
            except OSError:
                from .process_tree import process_gone
                return process_gone(pin.pid)
        return bool(pin.control(None, 1, 0))

    def wait(self, pin, seconds):
        if isinstance(pin, int):
            return bool(select.select([pin], [], [], seconds)[0])
        if isinstance(pin, BirthPin):
            deadline = time.monotonic() + seconds
            while not self.exited(pin):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.02, remaining))
            return True
        return bool(pin.control(None, 1, seconds))

    def send(self, pin, pid, number):
        if isinstance(pin, int):
            signal.pidfd_send_signal(pin, number)
        else:
            os.kill(pid, number)

    def close(self, pin):
        if isinstance(pin, int):
            os.close(pin)
        else:
            pin.close()

    def sole_holder(self, pid, path):
        from .process_tree import process_gone
        probe = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            info = os.fstat(probe)
            inode = info.st_dev, info.st_ino
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                return False
        finally:
            os.close(probe)
        holders = set()
        started = time.monotonic()
        if sys.platform == 'darwin':
            library = _darwin_library()
            candidates = (ctypes.c_int * (MAX_SCAN_PROCESSES + 1))()
            size = library.proc_listpids(4, os.getuid(), candidates, ctypes.sizeof(candidates))
            if size <= 0 or size >= ctypes.sizeof(candidates) or size % 4:
                return False
            processes = [p for p in candidates[:size // 4] if p > 0]
        else:
            processes = (int(p.name) for p in Path('/proc').iterdir() if p.name.isdigit())
        for candidate in bounded_processes(processes):
            if time.monotonic() - started > SCAN_SECONDS:
                raise TakeoverError('predecessor_unproven')
            try:
                if sys.platform == 'darwin':
                    fds = ctypes.create_string_buffer(FD_SCAN_BYTES)
                    size = library.proc_pidinfo(candidate, 1, 0, fds, len(fds))
                    if size <= 0 or size >= len(fds) or size % 8:
                        if process_gone(candidate):
                            continue
                        return False
                    for fd, kind in struct.iter_unpack('iI', fds.raw[:size]):
                        if kind != 1:  # PROX_FDTYPE_VNODE
                            continue
                        # proc_fileinfo (24) + vnode_info (152) + MAXPATHLEN
                        vnode = ctypes.create_string_buffer(1200)
                        got = library.proc_pidfdinfo(candidate, fd, 2, vnode, len(vnode))
                        if got != len(vnode):
                            if ctypes.get_errno() == errno.EBADF:
                                continue  # descriptor was closed during the scan
                            return False
                        found = struct.unpack_from('I', vnode, 24)[0], struct.unpack_from('Q', vnode, 32)[0]
                        if found == inode:
                            holders.add(candidate)
                else:
                    root = Path('/proc') / str(candidate)
                    if root.stat().st_uid != os.getuid():
                        continue
                    for fd in (root / 'fd').iterdir():
                        try:
                            info = fd.stat()
                        except FileNotFoundError:
                            continue
                        if (info.st_dev, info.st_ino) == inode:
                            holders.add(candidate)
            except (OSError, ValueError):
                if not process_gone(candidate):
                    return False
            if holders - {pid}:
                return False
        return holders == {pid}


def _trusted(identity, entry, state_root, project_root):
    argv = identity['argv']
    # Fixed interpreter flags only; arbitrary -c/-m programs are never entries.
    index = 1
    while index < len(argv) and argv[index] in ('-I', '-B', '-S', '-u'):
        index += 1
    if argv[index:index + 2] == ['-X', 'pycache_prefix=/dev/null']:
        index += 2
    return (identity['uid'] == os.getuid() and index < len(argv)
            and Path(argv[index]).resolve() == entry.resolve()
            and argv[index + 1:] == ['--state-root', str(state_root), '--project-root', str(project_root)])


class PinnedPredecessor:
    def __init__(self, kernel, pin, pid, identity):
        self.kernel, self.pin, self.pid, self.identity = kernel, pin, pid, identity

    def close(self):
        if self.pin is not None:
            self.kernel.close(self.pin)
            self.pin = None

    def _send(self, number):
        # Recheck before *each* signal, including escalation on macOS.
        current = self.kernel.identity(self.pid)
        if current != self.identity or self.kernel.exited(self.pin):
            raise TakeoverError('predecessor_unproven', phase='service_stopped')
        self.kernel.send(self.pin, self.pid, number)

    def stop(self):
        try:
            self._send(signal.SIGTERM)
            if self.kernel.wait(self.pin, 30.0):
                return False
            self._send(signal.SIGKILL)
            if not self.kernel.wait(self.pin, 5.0):
                raise TakeoverError('service_stop_failed', phase='service_stopped')
            return True
        finally:
            self.close()


def pin_predecessor(pid, *, entry, state_root, project_root, history_lock, kernel=None):
    kernel = Kernel() if kernel is None else kernel
    pin = None
    try:
        if type(pid) is not int or pid <= 0:
            raise ValueError()
        pin = kernel.pin(pid)
        identity = kernel.identity(pid)
        if (not _trusted(identity, entry, state_root, project_root)
                or not kernel.sole_holder(pid, history_lock)
                or kernel.identity(pid) != identity or kernel.exited(pin)):
            raise ValueError()
        return PinnedPredecessor(kernel, pin, pid, identity)
    except (OSError, ValueError, KeyError, TypeError, TakeoverError):
        if pin is not None:
            kernel.close(pin)
        raise TakeoverError('predecessor_unproven') from None
