"""Shared ``urllib`` JSON-over-HTTP base for the service clients.

:mod:`qbittorrent`, :mod:`plex`, and :mod:`arr` each carried a near-identical
``urllib`` core: opener construction (TLS-verify toggle + the fail-closed
:class:`~unraid_cache_cleaner.http_redirect.HostBoundRedirectHandler` + a
User-Agent), a request/read/decode wrapper, the
``HTTPError``/``URLError``/``OSError`` -> ``*ClientError`` taxonomy, and JSON
decode. :class:`JsonHttpClient` is that core, extracted once (see #20) so a fix
to any one path — a redirect-guard tweak, connect-timeout tuning, a
token-in-logs guarantee — lands for all three instead of being hand-propagated.

A subclass sets :attr:`service_name` and :attr:`error_class`, supplies its auth
headers via :meth:`_auth_headers` (and any extra opener handlers via
:meth:`_extra_handlers` — qBittorrent adds a cookie processor), and calls
:meth:`_get_json` (Plex/Radarr/Sonarr) or the lower-level :meth:`_read_text`
(qBittorrent, which keeps its own text/form-POST/403-reauth flow). Each client
keeps its own ``*ClientError`` subclass and its exact messages/special status
codes by overriding the ``_on_*_error`` hooks; the base only supplies the
plumbing and sensible defaults.

Stdlib-only and 3.9-compatible, matching the rest of the package.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Type

from . import USER_AGENT
from .http_redirect import build_handler

LOGGER = logging.getLogger(__name__)

#: HTTP methods a transient-failure retry is allowed to replay. Only idempotent
#: reads qualify: replaying a mutating call (qBittorrent's form-POST login) could
#: double-apply it, so a POST is always single-attempt regardless of
#: ``max_attempts``.
_RETRYABLE_METHODS = frozenset({"GET", "HEAD"})

#: Base backoff between retries; the delay grows exponentially per attempt
#: (``base``, ``base*2``, ...). Kept small since the target is a LAN service.
_DEFAULT_BACKOFF_SECONDS = 0.5


class JsonHttpError(RuntimeError):
    """Fallback ``*ClientError`` for a subclass that omits :attr:`error_class`.

    Every real client sets its own ``*ClientError``; this only exists so the
    base's ``error_class(message, *, status_code=None)`` contract holds even for
    a subclass that forgot to, surfacing a plain error instead of an opaque
    ``TypeError`` on the first non-2xx response.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class JsonHttpClient:
    """Shared ``urllib`` plumbing for the read-oriented service clients.

    The opener is fail-closed: it installs the host-bound redirect guard so the
    auth header (or cookie) added via ``addheaders`` can never be re-applied to a
    cross-host or TLS-downgrading redirect target, and honours the per-client
    ``verify_tls`` toggle for ``https`` bases. Subclasses override the class
    attributes and the hook methods below.
    """

    #: Human-readable label for the redirect guard and error messages. Never
    #: interpolate a secret into it.
    service_name: str = ""
    #: The subclass's ``*ClientError`` type. Must accept
    #: ``error_class(message, *, status_code=None)`` — every client's does. The
    #: default satisfies that contract so a forgetful subclass fails loudly, not
    #: with an opaque ``TypeError``.
    error_class: Type[RuntimeError] = JsonHttpError

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: int,
        verify_tls: bool,
        max_attempts: int = 1,
        backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.verify_tls = verify_tls
        # ``max_attempts`` is the total tries per idempotent request (1 = the
        # historical single-attempt behavior, i.e. retries off). Clamped to >= 1
        # so a misconfigured 0/negative never disables the request entirely.
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._opener = self._build_opener()

    # -- subclass hooks ----------------------------------------------------- #

    def _auth_headers(self) -> Sequence[Tuple[str, str]]:
        """Header pairs prepended to ``addheaders`` (the User-Agent is appended).

        These are *unredirected* headers urllib re-applies to every request it
        opens, which is why the redirect guard is mandatory. Return the auth
        credential(s) here; never place them in a URL.
        """

        return ()

    def _extra_handlers(self) -> Sequence[urllib.request.BaseHandler]:
        """Additional opener handlers (qBittorrent adds an ``HTTPCookieProcessor``)."""

        return ()

    # -- opener ------------------------------------------------------------- #

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: List[urllib.request.BaseHandler] = list(self._extra_handlers())
        handlers.append(
            build_handler(
                self.base_url,
                service_name=self.service_name,
                error_factory=self.error_class,
            )
        )

        if urllib.parse.urlparse(self.base_url).scheme == "https":
            context = ssl.create_default_context()
            if not self.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            handlers.append(urllib.request.HTTPSHandler(context=context))

        opener = urllib.request.build_opener(*handlers)
        opener.addheaders = [*self._auth_headers(), ("User-Agent", USER_AGENT)]
        return opener

    # -- request ------------------------------------------------------------ #

    def _build_url(self, api_path: str, params: Optional[Dict[str, str]] = None) -> str:
        url = f"{self.base_url}{api_path}"
        encoded_params = urllib.parse.urlencode(params or {})
        if encoded_params:
            url = f"{url}?{encoded_params}"
        return url

    # -- error taxonomy (override to customize wording / special codes) ----- #

    def _on_http_error(self, exc: urllib.error.HTTPError) -> Exception:
        """Map a non-2xx response to ``error_class``. Carries ``status_code`` so
        callers can branch (e.g. Plex skips a 404 section)."""

        body = exc.read().decode("utf-8", errors="replace")
        return self.error_class(
            f"HTTP {exc.code} from {self.service_name}: {body}", status_code=exc.code
        )

    def _on_url_error(self, exc: urllib.error.URLError) -> Exception:
        """Map a connect-phase failure (DNS/refused/handshake) to ``error_class``."""

        return self.error_class(
            f"Unable to connect to {self.service_name} at {self.base_url}: {exc.reason}"
        )

    def _on_os_error(self, exc: OSError) -> Exception:
        """Map a read-phase ``OSError`` (``socket.timeout``/dropped connection,
        which urllib does *not* wrap in ``URLError``) to ``error_class`` so a
        read-only report degrades instead of crashing."""

        return self.error_class(
            f"Unable to reach {self.service_name} at {self.base_url}: {exc}"
        )

    def _read_text(self, request: urllib.request.Request) -> str:
        """Open ``request`` through the opener and return the decoded body.

        Every transport failure is normalized onto ``error_class`` via the
        ``_on_*_error`` hooks. ``HTTPError`` is a subclass of ``URLError`` which
        is a subclass of ``OSError``, so the ``except`` order is significant.

        A transient transport failure — a 5xx response, a connect-phase
        ``URLError``, or a read-phase ``OSError`` (timeout / dropped connection) —
        is retried up to :attr:`max_attempts` times with exponential backoff, but
        *only* for an idempotent method (:data:`_RETRYABLE_METHODS`): replaying a
        POST could double-apply it, so qBittorrent's login stays single-attempt.
        A 4xx is never retried (it is deterministic and, for qBittorrent's 403,
        drives the one-shot reauth). When the attempts are exhausted the mapped
        ``error_class`` from the final failure propagates unchanged.
        """

        idempotent = request.get_method() in _RETRYABLE_METHODS
        attempt = 1
        while True:
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    return response.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                mapped, cause = self._on_http_error(exc), exc
                retryable = idempotent and exc.code >= 500
            except urllib.error.URLError as exc:
                mapped, cause = self._on_url_error(exc), exc
                retryable = idempotent
            except OSError as exc:
                mapped, cause = self._on_os_error(exc), exc
                retryable = idempotent

            if retryable and attempt < self.max_attempts:
                delay = self.backoff_seconds * (2 ** (attempt - 1))
                LOGGER.debug(
                    "%s request to %s failed (attempt %s/%s), retrying in %.2fs: %s",
                    self.service_name,
                    request.get_full_url(),
                    attempt,
                    self.max_attempts,
                    delay,
                    cause,
                )
                self._sleep(delay)
                attempt += 1
                continue
            raise mapped from cause

    def _get_json(
        self,
        api_path: str,
        params: Optional[Dict[str, str]] = None,
        *,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> object:
        """GET ``api_path`` and decode the JSON body.

        Returns the parsed value (``dict`` or ``list`` depending on the
        endpoint); a body that is not valid JSON becomes an ``error_class``.
        """

        request = urllib.request.Request(
            self._build_url(api_path, params),
            method="GET",
            headers=dict(extra_headers or {}),
        )
        body = self._read_text(request)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise self.error_class(
                f"{self.service_name} returned invalid JSON from {api_path}: {exc}"
            ) from exc

    def _ensure_json_object(self, payload: object, api_path: str) -> dict:
        """Assert a decoded JSON body is an object, else raise ``error_class``.

        :meth:`_get_json` returns whatever the endpoint sent — an object *or* a
        top-level array. Callers that require an object (Plex, whose responses are
        all ``{"MediaContainer": ...}``) route the decoded value through here so a
        non-object body surfaces as the client's ``*ClientError`` naming
        ``api_path``, rather than an ``AttributeError`` on a later ``.get(...)``.
        ``arr`` endpoints, which legitimately return top-level lists, keep calling
        :meth:`_get_json` directly and are unaffected.
        """

        if not isinstance(payload, dict):
            raise self.error_class(
                f"{self.service_name} returned a non-object JSON body from {api_path} "
                f"(expected an object, got {type(payload).__name__})"
            )
        return payload
