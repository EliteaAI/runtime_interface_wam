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

""" The whole WAM path in one process: client -> interface -> relay -> engine -> dummy provider

Every hop is the shipped code except two, and both are named where they are built: the arbiter
stream node (fixtures/relay.py — queues instead of pickling across pylons) and the usage plugin's
insert (fixtures/recording_hooks.py — a list instead of a table). The provider is
fixtures/dummy_wam_server.py, a real HTTP server on an ephemeral port, so the engine's own requests
call, the cookie auth, the url rewrite and the chunked SSE framing are all exercised for real.

What this adds over the unit and integration suites: those assert the interface calls the usage
plugin correctly. These assert a row comes out the far end with the right provider, dialect, token
source, project and run id — and that the paths which must not produce one do not.

Needs runtime_engine_wam checked out in this deployment's pylon_indexer; the module skips otherwise.
The dialect and token assertions additionally want the usage plugin next door, and say so when it
is absent.

To reproduce by hand instead, run the provider standalone and point a pylon at it:

    python3 tests/fixtures/dummy_wam_server.py     # listens on :8098
    WAM_INFERENCE_ENDPOINT=http://host.docker.internal:8098
    WAM_AUTH_ENDPOINT=http://host.docker.internal:8098/wam/auth
    WAM_USERID=local WAM_PASSWORD=local
"""

import importlib
import json
import pathlib
import sys
import types
import uuid

import flask
import pytest

from fixtures import dummy_wam_server, relay
from fixtures.helpers import bind, fake_module, load
from fixtures.recording_hooks import MODE_OFF, RecordingHooks

routes_proxy = load("routes.proxy")
methods_proxy = load("methods.proxy")

RUN_ID_HEADER = methods_proxy.ELITEA_RUN_ID_HEADER

URL_PREFIX = "/llm"
HTTP_METHODS = ["OPTIONS", "HEAD", "GET", "POST", "PUT", "PATCH", "DELETE"]

PROJECT_ID = 2
USER_ID = 7
USER_NAME = "e2e"

MODEL = "gpt-4o-mini"

ENGINE_PLUGINS_DIR = pathlib.Path(__file__).resolve().parents[5] / "pylon_indexer" / "plugins"


def engine_module(name):
    """A runtime_engine_wam submodule, or skip the module: the engine half is its own repo."""
    if not (ENGINE_PLUGINS_DIR / "runtime_engine_wam" / "module.py").is_file():
        pytest.skip(
            "runtime_engine_wam is not checked out at %s" % ENGINE_PLUGINS_DIR,
            allow_module_level=True,
        )
    #
    plugins_dir = str(ENGINE_PLUGINS_DIR)
    #
    if plugins_dir not in sys.path:
        sys.path.insert(0, plugins_dir)
    #
    return importlib.import_module("runtime_engine_wam.%s" % name)


engine_methods = engine_module("methods.proxy")


def interface_config(dummy):
    """config.yml as deployed, pointed at the dummy instead of a real WAM."""
    return {
        "wam_auth_endpoint": dummy.auth_url,
        "wam_userid": "local",
        "wam_password": "local",
        "wam_inference_endpoint": dummy.base_url,
        "wam_api_version": "2024-02-01",
        "wam_appid": None,
        #
        "proxy_ssl_verify": False,
        "proxy_connect_timeout": 5,
        "proxy_read_timeout": 30,
        "proxy_chunk_size": None,
        #
        "url_prefix": URL_PREFIX,
        "proxy_consumer_timeout": 30,
    }


class Rig:  # pylint: disable=R0903
    """The three things a test drives: the client, the provider's log, the recorded rows."""

    def __init__(self, client, dummy, hooks):
        self.client = client
        self.dummy = dummy
        self.hooks = hooks

    def post(self, body, headers=None):
        return self.client.post(
            "%s/v1/chat/completions" % URL_PREFIX, json=body, headers=headers or {},
        )

    def completion(self, body, headers=None):
        """A completion call, served to the last byte."""
        response = self.post(body, headers)
        #
        return response, response.get_data()

    def sent(self):
        """The body the provider actually received for the last completion call."""
        return self.dummy.completions()[-1]["payload"]

    def sent_headers(self):
        return self.dummy.completions()[-1]["headers"]

    def sent_path(self):
        return self.dummy.completions()[-1]["path"]

    def row(self):
        assert len(self.hooks.rows) == 1, self.hooks.rows
        #
        return self.hooks.rows[0]


@pytest.fixture(name="dummy", scope="module")
def dummy_fixture():
    dummy = dummy_wam_server.start()
    #
    yield dummy
    #
    dummy.stop()


@pytest.fixture(name="build_rig")
def build_rig_fixture(dummy, monkeypatch):
    """Both plugins, wired to each other over the relay and to the dummy over real HTTP."""
    def build(mode="elitea", project_id=PROJECT_ID):
        dummy.clear()
        #
        config = interface_config(dummy)
        hooks = RecordingHooks(mode=mode, project_id=project_id)
        #
        stream_node = relay.StreamNode()
        #
        engine = bind(fake_module(), engine_methods.Method)
        engine.stream_node = stream_node
        #
        interface = bind(fake_module(config=config), methods_proxy.Method, routes_proxy.Route)
        interface.stream_node = stream_node
        interface.service_node = relay.ServiceNode(wam_request_start=engine.wam_request_start)
        #
        monkeypatch.setattr(
            routes_proxy, "this",
            types.SimpleNamespace(descriptor=types.SimpleNamespace(config=config)),
        )
        monkeypatch.setattr(
            routes_proxy, "auth",
            types.SimpleNamespace(current_user=lambda: {
                "id": USER_ID, "email": "e2e@centry.user", "name": USER_NAME,
            }),
        )
        monkeypatch.setattr(routes_proxy, "usage_hooks", lambda: hooks)
        monkeypatch.setattr(methods_proxy, "usage_hooks", lambda: hooks)
        #
        app = flask.Flask(__name__)
        app.add_url_rule(
            "%s/<path:url>" % URL_PREFIX, "wam_route_http",
            interface.wam_route_http, methods=HTTP_METHODS,
        )
        #
        @app.before_request
        def _pat_auth():  # pylint: disable=W0612
            flask.g.auth = types.SimpleNamespace(type="pat")
        #
        return Rig(app.test_client(), dummy, hooks)
    #
    return build


def usage_frames(body):
    """The SSE data frames that carry a usage block."""
    frames = []
    #
    for line in body.decode("utf-8").splitlines():
        if not line.startswith("data: "):
            continue
        #
        payload = line[len("data: "):]
        #
        if payload != "[DONE]":
            frames.append(json.loads(payload))
    #
    return [frame for frame in frames if "usage" in frame]


class TestMeteredCall:
    """A PAT call straight at /llm/v1 — the traffic nothing else audits."""

    def test_a_non_streamed_call_is_metered_from_the_provider_tokens(self, build_rig):
        run_id = str(uuid.uuid4())
        rig = build_rig()
        #
        response, body = rig.completion(
            {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers={RUN_ID_HEADER: run_id},
        )
        #
        assert response.status_code == 200
        assert json.loads(body)["usage"]["prompt_tokens"] == 12
        #
        row = rig.row()
        #
        assert row["event_type"] == "llm"
        assert row["provider"] == routes_proxy.WAM_PROVIDER
        assert row["model_name"] == MODEL
        assert row["endpoint"] == "/v1/chat/completions"
        assert row["project_id"] == PROJECT_ID
        assert row["user_id"] == USER_ID
        assert row["run_id"] == run_id
        assert row["is_error"] is False
        #
        if not rig.hooks.reads_dialects:
            pytest.skip("usage plugin is not next door; dialect assertions skipped")
        #
        assert row["dialect"] == "wam.chat"
        assert row["token_source"] == "provider"
        assert row["input_tokens"] == 12
        assert row["output_tokens"] == 7

    def test_the_provider_sees_neither_the_run_id_header_nor_the_client_credentials(
            self, build_rig,
    ):
        rig = build_rig()
        #
        rig.completion(
            {"model": MODEL, "messages": []},
            headers={RUN_ID_HEADER: str(uuid.uuid4()), "Authorization": "Bearer pat-token"},
        )
        #
        sent = {key.lower() for key in rig.sent_headers()}
        #
        assert RUN_ID_HEADER.lower() not in sent
        assert "authorization" not in sent
        assert "cookie" in sent

    def test_a_malformed_run_id_is_dropped_rather_than_recorded(self, build_rig):
        """The header reaches us from a caller, so the row takes only a canonical uuid."""
        rig = build_rig()
        #
        rig.completion({"model": MODEL, "messages": []}, headers={RUN_ID_HEADER: "not-a-uuid"})
        #
        assert rig.row()["run_id"] is None

    def test_the_model_travels_in_the_url_not_the_body(self, build_rig):
        rig = build_rig()
        #
        rig.completion({"model": MODEL, "messages": []})
        #
        assert "model" not in rig.sent()
        assert rig.sent_path().startswith("/deployments/%s/chat/completions" % MODEL)

    def test_the_project_ladder_is_asked_with_the_service_user_prefix(self, build_rig):
        """Resolved on the request thread, while the client's own headers are still intact."""
        rig = build_rig()
        #
        rig.completion({"model": MODEL, "messages": []})
        #
        asked = rig.hooks.resolved[-1]
        #
        assert asked["user_id"] == USER_ID
        assert asked["user_name"] == USER_NAME
        assert asked["project_user_prefix"] == ":system:project:"


class TestStreamedCall:
    """The streamed side, where the tokens exist only because the interface asked for them."""

    def test_a_streamed_call_is_metered_from_the_injected_usage_frame(self, build_rig):
        rig = build_rig()
        #
        response, body = rig.completion({"model": MODEL, "messages": [], "stream": True})
        #
        assert response.status_code == 200
        assert rig.sent()["stream_options"] == {"include_usage": True}
        #
        # The dummy emits a usage frame only when asked, so its presence proves include_usage
        # survived the relay and reached the provider
        assert usage_frames(body)
        #
        row = rig.row()
        #
        assert row["is_error"] is False
        #
        if not rig.hooks.reads_dialects:
            pytest.skip("usage plugin is not next door; dialect assertions skipped")
        #
        assert row["dialect"] == "wam.chat"
        assert row["token_source"] == "provider"
        assert row["input_tokens"] == 12
        assert row["output_tokens"] == 7

    def test_nothing_on_this_path_buffers_the_body(self, build_rig):
        """The response is handed back streamed, and the row lands only once it is served."""
        rig = build_rig()
        #
        response = rig.post({"model": MODEL, "messages": [], "stream": True})
        #
        assert response.is_streamed
        assert rig.hooks.rows == []
        #
        response.get_data()
        #
        assert len(rig.hooks.rows) == 1


class TestUpstreamError:
    def test_an_upstream_error_is_recorded_as_one(self, build_rig):
        rig = build_rig()
        #
        response, body = rig.completion(
            {"model": MODEL, "messages": []}, headers={"X-Dummy-Error": "500"},
        )
        #
        assert response.status_code == 500
        assert json.loads(body)["error"]["type"] == "dummy_error"
        #
        row = rig.row()
        #
        assert row["is_error"] is True
        assert row["model_name"] == MODEL
        assert row["project_id"] == PROJECT_ID


class TestAuditedElsewhere:
    """The header every internal predict carries."""

    def test_a_call_audited_elsewhere_is_still_metered(self, build_rig):
        """It suppresses the legacy audit-trail span, which this path never emitted.

        elitea_core stamps the header on every internal predict, so suppressing the usage row
        on it would leave the platform's own traffic unmetered.
        """
        rig = build_rig()
        #
        response, body = rig.completion(
            {"model": MODEL, "messages": []}, headers={"X-Elitea-Audited": "1"},
        )
        #
        assert response.status_code == 200
        assert json.loads(body)["usage"]["prompt_tokens"] == 12
        assert rig.row()["model_name"] == MODEL


class TestNotMetered:
    """Two paths that must never produce a row, for two different reasons."""

    def test_the_models_endpoint_produces_no_row_and_never_reaches_the_provider(self, build_rig):
        rig = build_rig()
        #
        response = rig.client.get("%s/v1/models" % URL_PREFIX)
        #
        assert response.status_code == 200
        assert response.get_json() == {"data": [], "object": "list"}
        assert rig.dummy.served == []
        assert rig.hooks.rows == []

    def test_metering_off_leaves_the_wire_alone_and_writes_nothing(self, build_rig):
        rig = build_rig(mode=MODE_OFF)
        #
        response, body = rig.completion({"model": MODEL, "messages": [], "stream": True})
        #
        assert response.status_code == 200
        #
        # Not asked for, so not sent, so not reported: the client's stream is what it would have
        # been with no usage plugin in the process at all
        assert "stream_options" not in rig.sent()
        assert usage_frames(body) == []
        #
        assert rig.hooks.resolved == []
        assert rig.hooks.rows == []
