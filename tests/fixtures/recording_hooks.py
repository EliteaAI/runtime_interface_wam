#!/usr/bin/python3
# coding=utf-8

#   Copyright 2026 EPAM Systems
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

""" A recording stand-in for `tools.usage_hooks`

The usage plugin owns the metering policy and the insert; both live in another repo, and this one
must stay testable without it. So the row is kept in memory instead of written, and everything that
decides what goes in it is either the real thing or a marked mirror of it:

    dialect + tokens      real usage.sources library, when the plugin is checked out next door
    request_usage_frame   mirrors usage/interface.py::request_usage_frame
    meter_llm_response    mirrors usage/hooks.py::_metered / _record / _reading_of / _row

Keep the mirrors in step with their originals. Everything a mirror covers is a handful of lines;
what they exist to test is that the interface calls them at all, with the right arguments, on the
right paths -- not the policy itself.
"""

import dataclasses
import pathlib
import sys
import time
import typing
import uuid

MODE_OFF = "off"

EVENT_TYPE_LLM = "llm"
RUN_ID_HEADER = "X-Elitea-Run-Id"
HEAD_LIMIT = 8192
TOKEN_SOURCE_UNPARSED = "unparsed"

# usage/interface.py::STREAM_USAGE_PATHS
STREAM_USAGE_PATHS = ("/chat/completions", "/completions")

USAGE_PLUGIN_DIR = pathlib.Path(__file__).resolve().parents[3] / "usage"


def usage_sources():
    """The real usage.sources dialect registry, or None when the usage plugin is not next door.

    Imported as a top-level `sources` package, not as `usage.sources`: usage/__init__.py pulls in
    the plugin Module, which wants a live pylon and a database. The library itself needs neither.
    """
    if not (USAGE_PLUGIN_DIR / "sources" / "registry.py").is_file():
        return None
    #
    plugin_dir = str(USAGE_PLUGIN_DIR)
    #
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    #
    from sources import registry  # pylint: disable=C0415,E0401
    #
    if not registry.all():
        registry.register_defaults()
    #
    return registry


@dataclasses.dataclass
class UsageContext:  # pylint: disable=R0902
    """The fields of usage/hooks.py::UsageContext this rig reads back."""

    project_id: typing.Optional[int] = None
    user_id: typing.Optional[int] = None
    model_name: typing.Optional[str] = None
    endpoint: typing.Optional[str] = None
    provider: typing.Optional[str] = None
    run_id: typing.Optional[str] = None
    idempotency_key: typing.Optional[str] = None
    start_time_ns: typing.Optional[int] = None


class RecordingHooks:
    """What `tools.usage_hooks` looks like to the WAM interface, with the rows kept in a list."""

    def __init__(self, mode="elitea", project_id=2, registry=None):
        self.mode = mode
        self.project_id = project_id
        self.registry = usage_sources() if registry is None else registry
        #
        self.rows = []
        self.resolved = []
        self.began = []

    @property
    def reads_dialects(self):
        """Whether tokens and dialect ids in the rows came from the real library."""
        return self.registry is not None

    def usage_get_mode(self):
        return self.mode

    def usage_resolve_project_id(
            self, user_id, user_name=None, headers=None, project_user_prefix=None,
    ):
        self.resolved.append({
            "user_id": user_id,
            "user_name": user_name,
            "project_user_prefix": project_user_prefix,
        })
        #
        return self.project_id

    def request_usage_frame(self, proxy_target):
        """Mirrors usage/interface.py::request_usage_frame -- forced, not defaulted."""
        body = proxy_target.get("json")
        #
        if not isinstance(body, dict) or not body.get("stream"):
            return
        #
        path = (proxy_target.get("endpoint") or "").split("?", 1)[0].rstrip("/")
        #
        if not path.endswith(STREAM_USAGE_PATHS):
            return
        #
        options = body.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        options["include_usage"] = True
        body["stream_options"] = options

    def begin_llm_call(  # pylint: disable=R0913
            self, project_id=None, user_id=None, model_name=None, endpoint=None,
            headers=None, provider=None, run_id=None,
    ):
        """Mirrors usage/hooks.py::begin_llm_call, minus the project ladder it never reaches here."""
        if self.mode == MODE_OFF:
            return None
        #
        context = UsageContext(
            project_id=project_id if project_id else self.project_id,
            user_id=user_id,
            model_name=model_name,
            endpoint=endpoint,
            provider=provider,
            run_id=_run_id(run_id if run_id else (headers or {}).get(RUN_ID_HEADER)),
            idempotency_key=uuid.uuid4().hex,
            start_time_ns=time.monotonic_ns(),
        )
        #
        self.began.append(context)
        #
        return context

    def meter_llm_response(self, usage_context, response, iterator):
        if usage_context is None:
            return iterator
        #
        return self._metered(usage_context, response, iterator)

    def _metered(self, context, response, iterator):
        """Mirrors usage/hooks.py::_metered: probe the first chunk, pass every chunk through."""
        status = _status_of(response)
        dialect = None
        probed = False
        #
        try:
            for chunk in iterator:
                if not probed:
                    probed = True
                    dialect = self._match(context, response, chunk)
                #
                if dialect is not None:
                    dialect.feed(chunk)
                #
                yield chunk
        #
        finally:
            self.rows.append(_row(context, _reading_of(dialect), status))

    def _match(self, context, response, chunk):
        if self.registry is None:
            return None
        #
        return self.registry.match(
            context.endpoint, _content_type_of(response), _head_of(chunk),
            provider=context.provider,
        )


def _reading_of(dialect):
    """Mirrors usage/hooks.py::_reading_of, as a plain dict so the library stays optional."""
    if dialect is None:
        return {"dialect": None, "token_source": TOKEN_SOURCE_UNPARSED}
    #
    reading = dialect.result()
    token_source = reading.token_source
    #
    if reading.input_tokens is None and reading.output_tokens is None:
        token_source = TOKEN_SOURCE_UNPARSED
    #
    return {
        "dialect": reading.dialect,
        "model_name": reading.model_name,
        "input_tokens": reading.input_tokens,
        "output_tokens": reading.output_tokens,
        "cache_read_tokens": reading.cache_read_tokens,
        "cache_creation_tokens": reading.cache_creation_tokens,
        "reasoning_tokens": reading.reasoning_tokens,
        "token_source": token_source,
    }


def _row(context, reading, status):
    """The usage_event payload, less the pricing the usage plugin adds from its own catalog."""
    return {
        "idempotency_key": context.idempotency_key,
        "project_id": context.project_id,
        "user_id": context.user_id,
        "run_id": context.run_id,
        "event_type": EVENT_TYPE_LLM,
        "model_name": context.model_name,
        "provider": context.provider,
        "dialect": reading.get("dialect"),
        "endpoint": context.endpoint,
        "input_tokens": reading.get("input_tokens") or 0,
        "output_tokens": reading.get("output_tokens") or 0,
        "token_source": reading["token_source"],
        "duration_ms": _elapsed_ms(context),
        "is_error": status >= 400,
    }


def _run_id(value):
    """Mirrors usage/hooks.py::_run_id -- a caller-supplied header is not trusted to be a uuid."""
    if not value:
        return None
    #
    try:
        return str(uuid.UUID(str(value)))
    except (AttributeError, TypeError, ValueError):
        return None


def _elapsed_ms(context):
    if context.start_time_ns is None:
        return None
    #
    return int((time.monotonic_ns() - context.start_time_ns) / 1_000_000)


def _head_of(chunk):
    if isinstance(chunk, (bytes, bytearray)):
        return bytes(chunk[:HEAD_LIMIT])
    #
    return b""


def _status_of(response):
    try:
        return int((response or {}).get("status_code") or 200)
    except (AttributeError, TypeError, ValueError):
        return 200


def _content_type_of(response):
    headers = (response or {}).get("headers") or {}
    #
    for key, value in headers.items():
        if str(key).lower() == "content-type":
            return value
    #
    return None
