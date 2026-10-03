"""One running SilverbowPDF per user session: a second launch hands its files
over and exits.

Opening a PDF from Explorer or an email starts SilverbowPDF.exe again. On a
shared multi-session host every extra copy is another ~100-150MB of the host's RAM,
so (user's call) the running copy opens the file in a new tab instead.

The handover is a local socket (a named pipe on Windows) whose name holds
the user AND the Windows session -- the same user logged on twice gets two
independent apps -- and which only that user can open (UserAccessOption):
on a shared host, another user's process must never be able to open files
in your window.

Pipe names are machine-wide, though, and Windows lets a second process
listen on a name already taken -- so before saying anything the newcomer
checks the listening process runs as this user in this session (Windows
names a pipe's server process). Anyone else's, and it runs on its own.

Protocol, one line each way: the running app says it's there, the newcomer
lets it come to the front (Windows otherwise refuses a background process
that) and sends the paths as a JSON list, the running app answers "ok". No
answer in time and the newcomer runs on its own: a slow or stuck window
must not swallow the file.
"""

from __future__ import annotations

import ctypes
import getpass
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

TIMEOUT_MS = 3000


def server_name() -> str:
    session = 0
    if sys.platform == "win32":
        number = ctypes.c_ulong(0)
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(number)):
            session = number.value
    user = re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser())
    return f"SilverbowPDF-{user}-{session}"


def _allow_foreground(pid: int) -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.AllowSetForegroundWindow(pid)


def _session_of(pid: int) -> Optional[int]:
    number = ctypes.c_ulong(0)
    if not ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(number)):
        return None
    return number.value


def _user_sid(pid: int) -> Optional[bytes]:
    """The account a process runs as (its token's user SID), or None."""
    from ctypes import wintypes
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.GetLengthSid.argtypes = [ctypes.c_void_p]
    process = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not process:
        return None
    token = wintypes.HANDLE()
    try:
        if not adv.OpenProcessToken(process, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return None
        try:
            size = wintypes.DWORD()
            adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            buffer = ctypes.create_string_buffer(size.value)
            if not adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
            return ctypes.string_at(sid, adv.GetLengthSid(sid))
        finally:
            k32.CloseHandle(token)
    finally:
        k32.CloseHandle(process)


def _server_is_ours(socket: QLocalSocket) -> Optional[int]:
    """The listening process's id, if it runs as this user in this session;
    else None. Pipe names are machine-wide on Windows and a second process
    may listen on a name already in use, so on a shared host someone else
    could be listening under ours: never hand file names to them."""
    if sys.platform != "win32":
        return os.getpid()  # no shared pipe namespace to worry about
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    pid = wintypes.ULONG(0)
    if not k32.GetNamedPipeServerProcessId(int(socket.socketDescriptor()), ctypes.byref(pid)):
        return None
    me = os.getpid()
    if _session_of(pid.value) != _session_of(me):
        return None
    mine, theirs = _user_sid(me), _user_sid(pid.value)
    return pid.value if mine is not None and mine == theirs else None


def send_to_running(paths: List[str], name: Optional[str] = None,
                    timeout_ms: int = TIMEOUT_MS) -> bool:
    """Hand `paths` to a SilverbowPDF already running in this session. True
    if it took them (this process should then exit); False if there's none,
    or it didn't answer in time."""
    socket = QLocalSocket()
    socket.connectToServer(name or server_name())
    if not socket.waitForConnected(timeout_ms):
        return False
    try:
        server_pid = _server_is_ours(socket)
        if server_pid is None:
            return False  # not ours: say nothing, and run as a copy of our own
        # Its first line says it's alive and listening.
        if not socket.waitForReadyRead(timeout_ms) or not socket.canReadLine():
            return False
        socket.readLine()
        _allow_foreground(server_pid)
        absolute = [str(Path(p).resolve()) for p in paths]
        socket.write(json.dumps(absolute).encode("utf-8") + b"\n")
        if not socket.waitForBytesWritten(timeout_ms):
            return False
        while not socket.canReadLine():
            if not socket.waitForReadyRead(timeout_ms):
                return False
        return bytes(socket.readLine()).strip() == b"ok"
    finally:
        socket.abort()


class InstanceServer(QObject):
    """Listens for later launches; `files_requested` carries their paths
    (possibly none: a plain launch just brings the window forward)."""

    files_requested = Signal(list)

    def __init__(self, name: Optional[str] = None, parent=None):
        super().__init__(parent)
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        # Two copies started at the same moment can both listen under the
        # name (Windows allows it); a later launch then reaches either, and
        # both are this user's. Not listening at all only costs the handover.
        self.listening = self._server.listen(name or server_name())

    def close(self) -> None:
        self._server.close()

    def _accept(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(socket.deleteLater)
            socket.write(f"{os.getpid()}\n".encode("ascii"))
            socket.flush()

    def _read(self, socket: QLocalSocket) -> None:
        if not socket.canReadLine():
            return
        try:
            paths = json.loads(bytes(socket.readLine()).decode("utf-8"))
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ValueError("not a list of paths")
        except ValueError:
            socket.abort()
            return
        socket.write(b"ok\n")
        socket.flush()
        socket.disconnectFromServer()
        self.files_requested.emit(paths)
