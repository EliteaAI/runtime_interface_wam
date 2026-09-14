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

""" prepare_request() parks what metering needs off the request thread, strips the run-id header,
and gates the calls into the usage plugin on metering mode -- never on whether the plugin exists.

What a usage frame looks like, and which paths get one, is the usage plugin's policy; here only the
delegation is asserted. """

import pytest

from fixtures.helpers import bind, fake_module, load

proxy_methods = load("methods.proxy")
metering = load("utils.metering")

Method = proxy_methods.Method


def make_instance():
    return bind(fake_module(), Method)


def make_target(json_body, extra_headers=None):
    headers = {"Authorization": "Bearer x", "Host": "example.com"}
    headers.update(extra_headers or {})
    return {
        "endpoint": "/v1/chat/completions",
        "headers": headers,
        "json": json_body,
    }


class TestPrepareRequest:
    def test_parks_the_raw_model_name(self, monkeypatch):
        monkeypatch.setattr(proxy_methods, "usage_mode", lambda hooks: metering.MODE_OFF)
        instance = make_instance()
        proxy_target = make_target({"model": "gpt-4o-mini"})
        proxy_auth = {}
        #
        result = instance.prepare_request(proxy_target, proxy_auth)
        #
        assert result is None
        assert proxy_auth[proxy_methods.PLATFORM_RAW_MODEL_AUTH_KEY] == "gpt-4o-mini"

    def test_parks_the_run_id_and_never_forwards_its_header(self, monkeypatch):
        monkeypatch.setattr(proxy_methods, "usage_mode", lambda hooks: metering.MODE_OFF)
        instance = make_instance()
        proxy_target = make_target(
            {"model": "gpt-4o-mini"},
            extra_headers={proxy_methods.ELITEA_RUN_ID_HEADER: "run-123"},
        )
        proxy_auth = {}
        #
        instance.prepare_request(proxy_target, proxy_auth)
        #
        assert proxy_methods.ELITEA_RUN_ID_HEADER not in proxy_target["headers"]
        assert proxy_auth[proxy_methods.PLATFORM_RUN_ID_AUTH_KEY] == "run-123"

    def test_rewrites_the_url_to_the_deployment_path(self, monkeypatch):
        monkeypatch.setattr(proxy_methods, "usage_mode", lambda hooks: metering.MODE_OFF)
        instance = make_instance()
        proxy_target = make_target({"model": "gpt-4o-mini"})
        #
        instance.prepare_request(proxy_target, {})
        #
        assert proxy_target["url"] == "/deployments/gpt-4o-mini/chat/completions"

    def test_errors_when_the_request_has_no_model(self, monkeypatch):
        monkeypatch.setattr(proxy_methods, "usage_mode", lambda hooks: metering.MODE_OFF)
        instance = make_instance()
        proxy_target = make_target({})
        #
        body, status = instance.prepare_request(proxy_target, {})
        assert status == 400
        assert "model" in body["error"]["message"]

    @pytest.mark.parametrize("mode", ["elitea", "on"])
    def test_asks_the_usage_plugin_for_a_frame_when_metering_is_on(self, monkeypatch, mode):
        asked = []
        monkeypatch.setattr(proxy_methods, "usage_mode", lambda hooks: mode)
        monkeypatch.setattr(proxy_methods, "resolve_project_id", lambda hooks, auth, headers: 7)
        monkeypatch.setattr(
            proxy_methods, "request_usage_frame", lambda hooks, target: asked.append(target),
        )
        instance = make_instance()
        proxy_target = make_target({"model": "gpt-4o-mini", "stream": True})
        #
        instance.prepare_request(proxy_target, {})
        #
        assert asked == [proxy_target]

    def test_parks_the_resolved_project_when_metering_is_on(self, monkeypatch):
        monkeypatch.setattr(proxy_methods, "usage_mode", lambda hooks: "elitea")
        monkeypatch.setattr(proxy_methods, "resolve_project_id", lambda hooks, auth, headers: 7)
        monkeypatch.setattr(proxy_methods, "request_usage_frame", lambda hooks, target: None)
        instance = make_instance()
        proxy_auth = {}
        #
        instance.prepare_request(make_target({"model": "gpt-4o-mini"}), proxy_auth)
        #
        assert proxy_auth[proxy_methods.PLATFORM_PROJECT_ID_AUTH_KEY] == 7

    def test_touches_nothing_of_the_usage_plugin_when_metering_is_off(self, monkeypatch):
        calls = []
        monkeypatch.setattr(proxy_methods, "usage_mode", lambda hooks: metering.MODE_OFF)
        monkeypatch.setattr(
            proxy_methods, "resolve_project_id",
            lambda hooks, auth, headers: calls.append("resolve"),
        )
        monkeypatch.setattr(
            proxy_methods, "request_usage_frame", lambda hooks, target: calls.append("frame"),
        )
        instance = make_instance()
        proxy_target = make_target({"model": "gpt-4o-mini", "stream": True})
        proxy_auth = {}
        #
        instance.prepare_request(proxy_target, proxy_auth)
        #
        assert calls == []
        assert proxy_methods.PLATFORM_PROJECT_ID_AUTH_KEY not in proxy_auth
        assert "stream_options" not in proxy_target["json"]
