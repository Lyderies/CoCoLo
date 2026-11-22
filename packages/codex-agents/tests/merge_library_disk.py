"""Test-only inode/namespace power-loss model behind merge_library.IOCall.

File flush persists bytes and mode. Directory flush persists only its name→inode
edges. A crash discards everything else; a cross-directory rename is therefore
not durable until both parents are flushed. SQLite FULL commits are atomic and
independently durable. This models persistence, not arbitrary filesystem bugs.
"""
from __future__ import annotations

import copy
import errno
import os
import stat
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from codex_agents import merge_library as ml


class ModelCrash(BaseException):
    """Power loss bypasses library error recovery, like process termination."""


class _Connection:
    def __init__(self, disk, path):
        self.disk, self.path, self.generation = disk, str(path), disk.generation
        if not disk.exists(path):
            disk.write(path, b'', 0o600)
        self.pending = copy.deepcopy(disk.databases.get(self.path, {'rows': {}, 'queue': {}}))
        self.answer = []

    def execute(self, sql, parameters=()):
        if sql.startswith(('PRAGMA ', 'CREATE TABLE ')):
            return self
        if sql.startswith('SELECT value FROM '):
            table = sql.split()[3]
            self.answer = [(value,) for _, value in sorted(self.pending[table].items())]
        elif sql.startswith('INSERT OR REPLACE INTO '):
            table = sql.split()[4]
            key, value = parameters
            self.pending[table][key] = value
            self.disk.write(self.path, repr(self.pending).encode(), 0o600)
        else:
            raise AssertionError('unmodeled SQLite statement: ' + sql)
        return self

    def fetchall(self):
        return self.answer

    def commit(self):
        if self.generation == self.disk.generation:
            self.disk.databases[self.path] = copy.deepcopy(self.pending)
            self.disk.flush(self.path)

    def rollback(self):
        if self.generation == self.disk.generation:
            self.pending = copy.deepcopy(self.disk.databases.get(self.path, {'rows': {}, 'queue': {}}))

    def close(self):
        pass


class MemoryDisk:
    def __init__(self, root, *, seed=True):
        self.root = Path(root)
        self.nodes = {}
        self.links = {}
        self.durable_nodes = {}
        self.durable_links = {}
        self.handles = {}
        self.databases = {}
        self.git_reads = {}
        self.generation = 0
        self.next_inode = 100
        self.next_fd = 100000
        self.device = self.root.stat().st_dev if seed else 1
        self.ancestors = {str(p): p.lstat() for p in self.root.parents} if seed else {}
        self.root_inode = self._new(stat.S_IFDIR, 0o755, None)
        if seed:
            self.nodes[self.root_inode]['mode'] = stat.S_IMODE(self.root.stat().st_mode)
            def visit(directory):
                for path in sorted(directory.iterdir()):
                    info = path.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        self.mkdir(path, stat.S_IMODE(info.st_mode))
                    elif stat.S_ISREG(info.st_mode):
                        self.write(path, path.read_bytes(), stat.S_IMODE(info.st_mode))
                    elif stat.S_ISLNK(info.st_mode):
                        parent, name = self._parent(path)
                        self.links[parent][name] = self._new(stat.S_IFLNK, 0o777, os.readlink(path))
                    else:
                        raise AssertionError('unsupported seed inode ' + str(path))
                    # Seed identities are the real fixture's identities; persisted
                    # directory records remain authoritative in the modeled world.
                    parent, name = self._parent(path)
                    allocated = self.links[parent][name]
                    self.nodes[info.st_ino] = self.nodes.pop(allocated)
                    self.links[parent][name] = info.st_ino
                    if allocated in self.links:
                        self.links[info.st_ino] = self.links.pop(allocated)
                    self.next_inode = max(self.next_inode, info.st_ino)
                    if stat.S_ISDIR(info.st_mode):
                        visit(path)
                    elif path.name == 'history.sqlite':
                        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
                            self.databases[str(path)] = {table: dict(connection.execute('SELECT id,value FROM ' + table))
                                                        for table in ('rows', 'queue')}

            visit(self.root)
        self.durable_nodes = copy.deepcopy(self.nodes)
        self.durable_links = copy.deepcopy(self.links)
        self.calls = []

    @classmethod
    def empty(cls, root):
        return cls(root, seed=False)

    def _new(self, kind, mode, data):
        self.next_inode += 1
        inode = self.next_inode
        self.nodes[inode] = dict(kind=kind, mode=mode, data=data)
        if kind == stat.S_IFDIR:
            self.links[inode] = {}
        return inode

    def _inode(self, path):
        inode = self.root_inode
        try:
            parts = Path(path).relative_to(self.root).parts
        except ValueError:
            raise AssertionError('model access outside fixture: ' + str(path)) from None
        for part in parts:
            if self.nodes[inode]['kind'] != stat.S_IFDIR:
                raise NotADirectoryError(errno.ENOTDIR, 'not directory', str(path))
            try:
                inode = self.links[inode][part]
            except KeyError:
                raise FileNotFoundError(errno.ENOENT, 'absent', str(path)) from None
        return inode

    def _parent(self, path):
        return self._inode(Path(path).parent), Path(path).name

    def exists(self, path):
        try:
            self._inode(path)
            return True
        except FileNotFoundError:
            return False

    def info(self, path=None, *, inode=None):
        inode = self._inode(path) if inode is None else inode
        node = self.nodes[inode]
        return SimpleNamespace(st_dev=self.device, st_ino=inode,
                               st_nlink=sum(list(names.values()).count(inode) for names in self.links.values()) or 1,
                               st_mode=node['kind'] | node['mode'], st_size=len(node['data'] or b''))

    def read(self, path):
        node = self.nodes[self._inode(path)]
        if node['kind'] != stat.S_IFREG:
            raise IsADirectoryError(errno.EISDIR, 'not regular file', str(path))
        return node['data']

    def write(self, path, data, mode=0o644):
        if not self.exists(path):
            parent, name = self._parent(path)
            self.links[parent][name] = self._new(stat.S_IFREG, mode, b'')
        node = self.nodes[self._inode(path)]
        node.update(data=data, mode=mode)

    def mkdir(self, path, mode=0o755):
        if self.exists(path):
            raise FileExistsError(errno.EEXIST, 'exists', str(path))
        parent, name = self._parent(path)
        self.links[parent][name] = self._new(stat.S_IFDIR, mode, None)

    def unlink(self, path):
        parent, name = self._parent(path)
        if name not in self.links[parent]:
            raise FileNotFoundError(errno.ENOENT, 'absent', str(path))
        del self.links[parent][name]

    def rmdir(self, path):
        if self.links[self._inode(path)]:
            raise OSError(errno.ENOTEMPTY, 'not empty', str(path))
        self.unlink(path)

    def rename(self, source, target, exchange=False):
        a, an = self._parent(source)
        b, bn = self._parent(target)
        inode = self._inode(source)
        if exchange:
            other = self._inode(target)
            self.links[a][an], self.links[b][bn] = other, inode
        else:
            if bn in self.links[b]:
                raise FileExistsError(errno.EEXIST, 'exists', str(target))
            self.links[b][bn] = inode
            del self.links[a][an]

    def flush(self, path):
        inode = self._inode(path)
        self.durable_nodes[inode] = copy.deepcopy(self.nodes[inode])
        if inode in self.links:
            self.durable_links[inode] = dict(self.links[inode])

    def crash(self):
        self.nodes = copy.deepcopy(self.durable_nodes)
        self.links = copy.deepcopy(self.durable_links)
        # A persisted name cannot confer persisted contents on an unflushed inode.
        for names in self.links.values():
            for name, inode in list(names.items()):
                if inode not in self.nodes:
                    del names[name]
        for inode, node in self.nodes.items():
            if node['kind'] == stat.S_IFDIR:
                self.links.setdefault(inode, {})
        self.handles.clear()
        self.generation += 1

    def snapshot(self, root, *, control=True):
        result = {}
        def visit(inode, prefix):
            for name, child in sorted(self.links[inode].items()):
                if not control and not prefix and name in ('.git', '.cocolo'):
                    continue
                key = prefix + name
                node = self.nodes[child]
                result[key] = node['kind'], node['mode'], node['data']
                if node['kind'] == stat.S_IFDIR:
                    visit(child, key + '/')
        visit(self._inode(root), '')
        return result

    def __call__(self, call):
        self.calls.append(call)
        if call.when != 'before':
            return None
        op, path, detail = call.operation, call.path, call.detail
        answer = None
        if op == 'lstat':
            answer = self.ancestors.get(path)
            if answer is None:
                answer = self.info(path) if self.exists(path) else None
        elif op == 'listdir':
            answer = sorted(self.links[self._inode(path)])
        elif op == 'open':
            flags = detail['flags']
            exists = self.exists(path)
            if exists and flags & os.O_EXCL:
                raise FileExistsError(errno.EEXIST, 'exists', path)
            if not exists and flags & os.O_CREAT:
                self.write(path, b'', detail.get('mode', 0o644))
            inode = self._inode(path)
            self.next_fd += 1
            answer = self.next_fd
            self.handles[answer] = [inode, len(self.nodes[inode]['data'] or b'') if flags & os.O_APPEND else 0]
        elif op == 'close':
            self.handles.pop(detail['fd'], None)
        elif op == 'fstat':
            answer = self.info(inode=self.handles[detail['fd']][0])
        elif op == 'read':
            if 'fd' in detail:
                handle = self.handles[detail['fd']]
                data = self.nodes[handle[0]]['data']
                answer = data[handle[1]:handle[1] + detail['size']]
                handle[1] += len(answer)
            else:
                answer = self.read(path) if self.exists(path) else b''
        elif op == 'write':
            inode, offset = self.handles[detail['fd']]
            data = detail['data']
            node = self.nodes[inode]
            node['data'] = node['data'][:offset] + data + node['data'][offset + len(data):]
            self.handles[detail['fd']][1] += len(data)
            answer = len(data)
        elif op == 'chmod':
            self.nodes[self.handles[detail['fd']][0]]['mode'] = detail['mode']
        elif op in ('fsync', 'fullfsync'):
            self.flush(path)
        elif op == 'mkdir':
            self.mkdir(path)
        elif op == 'unlink':
            self.unlink(path)
        elif op == 'rmdir':
            self.rmdir(path)
        elif op in ('exchange', 'rename_noreplace'):
            self.rename(path, call.other, op == 'exchange')
        elif op == 'statvfs':
            answer = SimpleNamespace(f_bavail=10**9, f_frsize=4096)
        elif op == 'sqlite_open':
            answer = _Connection(self, path)
        elif op == 'merge_file':
            argv = detail['argv']
            owner, base, child = (self.read(p) for p in argv[-3:])
            merged = child if owner == base else owner if child == base or child == owner else None
            answer = subprocess.CompletedProcess(argv, 0 if merged is not None else 1, merged or b'', b'')
        elif op == 'git_read':
            argv = tuple(detail['argv'])
            if argv not in self.git_reads:
                self.git_reads[argv] = subprocess.run(['git', '-C', str(Path(path).parent), *argv], capture_output=True, check=True, timeout=8).stdout
            answer = self.git_reads[argv]
        elif op in ('sqlite_read', 'sqlite_write', 'sqlite_commit', 'sqlite_close', 'checkpoint', 'project_checks'):
            # Read-only git and trusted policy callbacks are deliberately real;
            # SQLite callbacks operate the model connection returned above.
            return None
        else:
            raise AssertionError('unmodeled IOCall: ' + repr(call))
        return ml.IOOverride(answer)
