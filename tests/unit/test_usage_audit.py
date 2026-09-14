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

""" is_audited_elsewhere() gates the double-counting suppression for WAM's own gateway """

import pytest

from fixtures.helpers import load

usage_audit = load("utils.usage_audit")


class TestIsAuditedElsewhere:
    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on", "  TRUE  "])
    def test_truthy_values(self, value):
        assert usage_audit.is_audited_elsewhere({usage_audit.INTERNAL_AUDIT_HEADER: value})

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "banana"])
    def test_falsy_values(self, value):
        assert not usage_audit.is_audited_elsewhere({usage_audit.INTERNAL_AUDIT_HEADER: value})

    def test_missing_header(self):
        assert not usage_audit.is_audited_elsewhere({})

    def test_never_raises_on_a_broken_headers_object(self):
        class Explodes:
            def get(self, *a, **k):
                raise RuntimeError("boom")
        #
        assert not usage_audit.is_audited_elsewhere(Explodes())
