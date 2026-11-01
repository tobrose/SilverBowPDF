"""Help > Check for Updates: is there a newer release on GitHub?

One request, only when the user asks -- never on startup, so opening a PDF
never waits on the network. It asks GitHub's web page for the latest
release, which answers with a redirect to that release (/releases/tag/v1.2.3)
and the version is read from where it points. Not GitHub's REST API: that
allows 60 requests an hour per network address without signing in, and on
shared desktops everyone is behind one address. The latest release leaves
out drafts and pre-releases. Only plain vX.Y.Z tags count as versions.

Qt's network stack does the fetch: it uses Windows' proxy settings (a
managed network often has one, which plain urllib would ignore) and its own
TLS, and doesn't block the window. Every outcome is reported, a failure
included (rule 7): "couldn't check, because ..." is an answer; silence isn't.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxyFactory, QNetworkReply, QNetworkRequest

from pdfapp import __version__

REPO = "tobrose/SilverBowPDF"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"
RELEASES_PAGE = RELEASES_URL
TIMEOUT_MS = 10000


class UpdateCheckError(Exception):
    pass


@dataclass(frozen=True)
class Release:
    version: str        # "0.9.0", from the tag "v0.9.0"
    page: str           # the release's web page


def version_tuple(version: str) -> Tuple[int, ...]:
    """"v0.8.1" or "0.8.1" -> (0, 8, 1). Anything else -- "v1.0.0-rc1",
    "latest" -- isn't a release version, and is an error rather than being
    misread as one."""
    plain = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    if plain is None:
        raise UpdateCheckError(f"GitHub's latest release isn't a plain version number ({version!r}).")
    return tuple(int(n) for n in plain.group(1).split("."))


def release_from_redirect(location: str) -> Release:
    """The release a /releases/latest redirect points at."""
    tag = re.search(r"/releases/tag/([^/?#]+)/?$", location)
    if tag is None:
        raise UpdateCheckError("GitHub has no published release for SilverbowPDF yet.")
    version = ".".join(str(n) for n in version_tuple(QUrl.fromPercentEncoding(tag.group(1).encode())))
    return Release(version, location)


def parse_release(data: bytes) -> Release:
    """A release as JSON ({"tag_name", "html_url"}): GitHub's API shape,
    which the tests serve from files."""
    try:
        release = json.loads(data.decode("utf-8"))
        tag = release["tag_name"]
    except (ValueError, KeyError, TypeError) as exc:
        raise UpdateCheckError(f"GitHub's answer didn't make sense ({exc}).") from exc
    version = ".".join(str(n) for n in version_tuple(str(tag)))
    return Release(version, str(release.get("html_url") or RELEASES_PAGE))


def newer_than_running(release: Release, current: str = __version__) -> bool:
    return version_tuple(release.version) > version_tuple(current)


class ReleaseCheck(QObject):
    """One check. `done` carries (Release, None) or (None, reason)."""

    done = Signal(object, object)

    def __init__(self, parent=None, url: Optional[str] = None):
        super().__init__(parent)
        QNetworkProxyFactory.setUseSystemConfiguration(True)
        self._manager = QNetworkAccessManager(self)
        self._url = url or RELEASES_URL
        self._reply: Optional[QNetworkReply] = None

    def start(self) -> None:
        request = QNetworkRequest(QUrl(self._url))
        # The redirect IS the answer: read where it points, don't follow it.
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                             QNetworkRequest.ManualRedirectPolicy)
        request.setRawHeader(b"User-Agent", f"SilverbowPDF/{__version__}".encode("ascii"))
        request.setTransferTimeout(TIMEOUT_MS)
        self._reply = self._manager.get(request)
        self._reply.finished.connect(self._finished)

    def _finished(self) -> None:
        reply, self._reply = self._reply, None
        reply.deleteLater()
        try:
            self.done.emit(self._release(reply), None)
        except UpdateCheckError as exc:
            self.done.emit(None, str(exc))

    @staticmethod
    def _release(reply: QNetworkReply) -> Release:
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        location = reply.attribute(QNetworkRequest.RedirectionTargetAttribute)
        if status in (301, 302, 303, 307, 308) and location is not None:
            return release_from_redirect(reply.url().resolved(location).toString())
        if status in (403, 429):
            raise UpdateCheckError("GitHub is turning requests away from this network for now "
                                   "(too many recently). Try again later.")
        if status == 404:
            raise UpdateCheckError("GitHub has no published release for SilverbowPDF yet.")
        if reply.error() != QNetworkReply.NoError:
            raise UpdateCheckError(reply.errorString())
        return parse_release(bytes(reply.readAll()))
