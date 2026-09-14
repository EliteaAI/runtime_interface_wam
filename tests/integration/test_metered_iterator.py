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

""" metered_iterator() must never break the proxy in the absence or failure of the usage plugin.

Integration-level because it loads routes.proxy as a real package member (its relative imports
require that) and drives it through an actual flask.request context, the way the real route
handler's own flask.request.headers access is exercised. """

import types

import flask
import pytest

from fixtures.helpers import load

routes_proxy = load("routes.proxy")

APP = flask.Flask(__name__)


def make_proxy_auth(**overrides):
    proxy_auth = {
        "user": {"id": 7},
        routes_proxy.PLATFORM_RAW_MODEL_AUTH_KEY: "gpt-4o-mini",
        routes_proxy.PLATFORM_PROJECT_ID_AUTH_KEY: 2,
        routes_proxy.PLATFORM_RUN_ID_AUTH_KEY: "run-123",
    }
    proxy_auth.update(overrides)
    return proxy_auth


class TestMeteredIterator:
    def test_identity_when_hooks_are_unavailable(self, monkeypatch):
        monkeypatch.setattr(routes_proxy, "usage_hooks", lambda: None)
        iterator = iter([b"a", b"b"])
        #
        with APP.test_request_context("/deployments/gpt-4o-mini/chat/completions"):
            result = routes_proxy.metered_iterator({}, make_proxy_auth(), {}, iterator)
        #
        assert result is iterator

    def test_identity_when_metering_is_off(self, monkeypatch):
        hooks = types.SimpleNamespace(
            usage_get_mode=lambda: routes_proxy.MODE_OFF,
            begin_llm_call=lambda **k: pytest.fail("should not meter with metering off"),
        )
        monkeypatch.setattr(routes_proxy, "usage_hooks", lambda: hooks)
        iterator = iter([b"a"])
        #
        with APP.test_request_context("/deployments/gpt-4o-mini/chat/completions"):
            result = routes_proxy.metered_iterator({}, make_proxy_auth(), {}, iterator)
        #
        assert result is iterator

    def test_identity_when_the_call_was_never_marked_billable(self, monkeypatch):
        """A path that never reaches the upstream -- /v1/models, a rejection -- parks no model."""
        hooks = types.SimpleNamespace(
            usage_get_mode=lambda: "elitea",
            begin_llm_call=lambda **k: pytest.fail("should not meter an unbilled path"),
        )
        monkeypatch.setattr(routes_proxy, "usage_hooks", lambda: hooks)
        iterator = iter([b"a"])
        proxy_auth = {"user": {"id": 7}}
        #
        with APP.test_request_context("/v1/models"):
            result = routes_proxy.metered_iterator({}, proxy_auth, {}, iterator)
        #
        assert result is iterator

    def test_meters_traffic_audited_elsewhere_all_the_same(self, monkeypatch):
        """X-Elitea-Audited suppresses the legacy audit span, never the usage row.

        Every internal predict carries the header, so honouring it here would leave the
        platform's own traffic unmetered -- the whole point of the usage plugin.
        """
        metered = iter([b"metered"])
        hooks = types.SimpleNamespace(
            usage_get_mode=lambda: "elitea",
            begin_llm_call=lambda **k: "usage-context",
            meter_llm_response=lambda *a: metered,
        )
        monkeypatch.setattr(routes_proxy, "usage_hooks", lambda: hooks)
        #
        with APP.test_request_context(
                "/deployments/gpt-4o-mini/chat/completions",
                headers={"X-Elitea-Audited": "true"},
        ):
            result = routes_proxy.metered_iterator({}, make_proxy_auth(), {}, iter([b"a"]))
        #
        assert result is metered

    def test_meters_through_the_hooks_when_available(self, monkeypatch):
        metered = iter([b"metered"])
        calls = {}

        def begin_llm_call(**kwargs):
            calls["begin"] = kwargs
            return "usage-context"

        def meter_llm_response(usage_context, response, iterator):
            calls["meter"] = (usage_context, response, iterator)
            return metered

        hooks = types.SimpleNamespace(
            usage_get_mode=lambda: "elitea",
            begin_llm_call=begin_llm_call, meter_llm_response=meter_llm_response,
        )
        monkeypatch.setattr(routes_proxy, "usage_hooks", lambda: hooks)
        #
        proxy_target = {"endpoint": "/v1/chat/completions"}
        response = {"status_code": 200}
        iterator = iter([b"raw"])
        #
        with APP.test_request_context("/deployments/gpt-4o-mini/chat/completions"):
            result = routes_proxy.metered_iterator(
                proxy_target, make_proxy_auth(), response, iterator,
            )
        #
        assert result is metered
        assert calls["begin"]["project_id"] == 2
        assert calls["begin"]["user_id"] == 7
        assert calls["begin"]["model_name"] == "gpt-4o-mini"
        assert calls["begin"]["endpoint"] == "/v1/chat/completions"
        assert calls["begin"]["provider"] == routes_proxy.WAM_PROVIDER
        assert calls["begin"]["run_id"] == "run-123"
        assert calls["meter"] == ("usage-context", response, iterator)

    def test_falls_back_to_the_raw_iterator_when_metering_raises(self, monkeypatch, recording_log):
        def explodes(**kwargs):
            raise RuntimeError("boom")
        #
        hooks = types.SimpleNamespace(
            usage_get_mode=lambda: "elitea",
            begin_llm_call=explodes, meter_llm_response=explodes,
        )
        monkeypatch.setattr(routes_proxy, "usage_hooks", lambda: hooks)
        iterator = iter([b"raw"])
        #
        with APP.test_request_context("/deployments/gpt-4o-mini/chat/completions"):
            result = routes_proxy.metered_iterator({}, make_proxy_auth(), {}, iterator)
        #
        assert result is iterator
        assert recording_log.messages("exception")
