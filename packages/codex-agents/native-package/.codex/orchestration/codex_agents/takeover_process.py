"""Pin and stop only a proven pre-B4t project service, never a collector or child.

Linux uses a pidfd when available. Older kernels use the last-birth-check rule,
as does macOS with NOTE_EXIT and NOTE_EXEC. The numeric signal's remaining PID reuse gap is
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

FD_SCAN_BYTES = 4 * 1024 * 1024


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
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int
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


class DarwinPin:
    def __init__(self, queue):
        self.queue, self.events = queue, 0

    def control(self, changes, count, timeout):
        events = self.queue.control(changes, count, timeout)
        for event in events:
            self.events |= event.fflags
        return events

    def close(self):
        self.queue.close()


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
                    flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE, fflags=select.KQ_NOTE_EXIT | select.KQ_NOTE_EXEC)], 0, 0)
                return DarwinPin(queue)
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
        _events = pin.control(None, 1, 0)
        return bool(pin.events & select.KQ_NOTE_EXIT)

    def changed(self, pin):
        if isinstance(pin, (int, BirthPin)):
            return self.exited(pin)
        pin.control(None, 1, 0)
        return bool(pin.events & (select.KQ_NOTE_EXEC | select.KQ_NOTE_EXIT))

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
        deadline = time.monotonic() + seconds
        while not pin.events & select.KQ_NOTE_EXIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            # NOTE_EXEC is not exit. Keep it latched for I4, but wait out the
            # existing stop budget rather than treating the event as timeout.
            pin.control(None, 1, remaining)
        return True

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
        if self.kernel.exited(self.pin):
            return False
        try:
            current = self.kernel.identity(self.pid)
        except OSError:
            if self.kernel.exited(self.pin):
                return False
            raise TakeoverError('predecessor_unproven', phase='service_stopped') from None
        if current != self.identity:
            raise TakeoverError('predecessor_unproven', phase='service_stopped')
        if self.kernel.changed(self.pin):
            if self.kernel.exited(self.pin):
                return False
            raise TakeoverError('predecessor_unproven', phase='service_stopped')
        try:
            self.kernel.send(self.pin, self.pid, number)
        except ProcessLookupError:
            return False
        return True

    def stop(self):
        try:
            if not self._send(signal.SIGTERM):
                return False
            if self.kernel.wait(self.pin, 30.0):
                return False
            if not self._send(signal.SIGKILL):
                return False
            if not self.kernel.wait(self.pin, 5.0):
                raise TakeoverError('service_stop_failed', phase='service_stopped')
            return True
        finally:
            self.close()


def pin_predecessor(pid, *, entry, state_root, project_root, kernel=None):
    kernel = Kernel() if kernel is None else kernel
    pin = None
    try:
        if type(pid) is not int or pid <= 0:
            raise ValueError()
        pin = kernel.pin(pid)
        identity = kernel.identity(pid)
        if (not _trusted(identity, entry, state_root, project_root)
                or kernel.identity(pid) != identity or kernel.changed(pin)):
            raise ValueError()
        return PinnedPredecessor(kernel, pin, pid, identity)
    except (OSError, ValueError, KeyError, TypeError, TakeoverError):
        if pin is not None:
            kernel.close(pin)
        raise TakeoverError('predecessor_unproven') from None


def foreign_lock_holder(pid, paths):
    """Linux I5: one system flock table; no process descriptors are inspected.

    An unreadable table uses the macOS exclusion rule. The owner still must
    acquire every lifetime flock before adopting either store.
    """
    if not sys.platform.startswith('linux'):
        return False
    inodes = set()
    for path in paths:
        try:
            info = Path(path).stat()
        except FileNotFoundError:
            continue
        inodes.add((os.major(info.st_dev), os.minor(info.st_dev), info.st_ino))
    try:
        with Path('/proc/locks').open(encoding='ascii') as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 6 or fields[1] != 'FLOCK':
                    continue
                major, minor, inode = fields[5].split(':')
                if ((int(major, 16), int(minor, 16), int(inode)) in inodes
                        and int(fields[4]) != pid):
                    return True
    except (OSError, ValueError):
        return False
    return False
