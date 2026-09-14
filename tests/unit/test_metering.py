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

""" usage_hooks()/usage_mode() must never break the proxy, on any resolution failure """

import sys
import types

import pytest

from fixtures.helpers import load

metering = load("utils.metering")


class TestUsageHooks:
    def test_returns_none_when_tools_has_no_usage_hooks(self, monkeypatch):
        stub_tools = types.SimpleNamespace()
        monkeypatch.setitem(sys.modules, "tools", stub_tools)
        #
        assert metering.usage_hooks() is None

    def test_returns_the_registered_hooks(self, monkeypatch):
        sentinel = types.SimpleNamespace()
        stub_tools = types.SimpleNamespace(usage_hooks=sentinel)
        monkeypatch.setitem(sys.modules, "tools", stub_tools)
        #
        assert metering.usage_hooks() is sentinel

    def test_returns_none_when_tools_itself_is_unimportable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "tools", None)
        #
        assert metering.usage_hooks() is None


class TestUsageMode:
    def test_off_when_hooks_is_none(self):
        assert metering.usage_mode(None) == metering.MODE_OFF

    def test_returns_whatever_the_hooks_report(self):
        hooks = types.SimpleNamespace(usage_get_mode=lambda: "elitea")
        #
        assert metering.usage_mode(hooks) == "elitea"

    @pytest.mark.parametrize("error", [RuntimeError("boom"), ValueError("boom")])
    def test_off_on_any_resolution_failure(self, error):
        def raise_it():
            raise error
        #
        hooks = types.SimpleNamespace(usage_get_mode=raise_it)
        #
        assert metering.usage_mode(hooks) == metering.MODE_OFF
