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

""" An in-process stand-in for the arbiter stream node the two WAM plugins relay over

The interface lives in pylon_main and the engine in pylon_indexer; in production they never share
a process and talk only through arbiter streams. The rig runs both in one interpreter over the
queues below, which implement the same surface each side actually uses:

    stream_node.add_stream() / remove_stream(id)
    stream_node.get_emitter(id)   -> .chunk(obj) / .end() / .exception(exception_info=...)
    stream_node.get_consumer(id, timeout=...) -> iterable of chunks

One deliberate difference from the real thing: arbiter pickles every chunk, so the two sides get
copies. Here they share objects by reference. That matters only for a test asserting that one side
mutated the other's dict -- assert on the bytes on the wire instead.
"""

import queue
import threading
import uuid

_CHUNK = "chunk"
_END = "end"
_EXCEPTION = "exception"


class RelayError(RuntimeError):
    """What a consumer raises when the far side reported an exception."""


class Emitter:  # pylint: disable=R0903
    """The writing end of one stream."""

    def __init__(self, channel):
        self._channel = channel

    def chunk(self, obj):
        self._channel.put((_CHUNK, obj))

    def end(self):
        self._channel.put((_END, None))

    def exception(self, exception_info=""):
        self._channel.put((_EXCEPTION, exception_info))


class Consumer:  # pylint: disable=R0903
    """The reading end of one stream. Lazy, so a streamed body is never buffered whole."""

    def __init__(self, channel, timeout):
        self._channel = channel
        self._timeout = timeout

    def __iter__(self):
        while True:
            kind, payload = self._channel.get(timeout=self._timeout)
            #
            if kind == _CHUNK:
                yield payload
            elif kind == _END:
                return
            else:
                raise RelayError(payload)


class StreamNode:
    """Enough of the arbiter stream node for the interface/engine pair."""

    def __init__(self):
        self._channels = {}
        self._lock = threading.Lock()

    def add_stream(self):
        stream_id = uuid.uuid4().hex
        #
        with self._lock:
            self._channels[stream_id] = queue.Queue()
        #
        return stream_id

    def remove_stream(self, stream_id):
        with self._lock:
            self._channels.pop(stream_id, None)

    def get_emitter(self, stream_id):
        return Emitter(self._channel(stream_id))

    def get_consumer(self, stream_id, timeout=600):
        return Consumer(self._channel(stream_id), timeout)

    def _channel(self, stream_id):
        with self._lock:
            return self._channels[stream_id]


class ServiceNode:  # pylint: disable=R0903
    """`self.service_node.call.wam_request_start(...)` as the interface route invokes it."""

    def __init__(self, **handlers):
        self.call = _Call(handlers)


class _Call:  # pylint: disable=R0903
    def __init__(self, handlers):
        self._handlers = handlers

    def __getattr__(self, name):
        try:
            return self._handlers[name]
        except KeyError:
            raise AttributeError(name)  # pylint: disable=W0707
