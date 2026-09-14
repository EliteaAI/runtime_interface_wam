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

""" Call sites into the usage plugin — no metering policy lives here

WAM knows its provider without asking (every model behind this interface is served by WAM), so
it drives the lower-level hooks directly rather than having the provider resolved per call. Every
other decision — the project ladder, the streamed-usage frame, what a row looks like — is the
usage plugin's, and is reached through the calls below. All of them are total: a metering failure
must never cost the caller their response.
"""

from pylon.core.tools import log  # pylint: disable=E0611,E0401,W0611

MODE_OFF = "off"

PLATFORM_PROJECT_ID_AUTH_KEY = "platform_project_id"
PLATFORM_RAW_MODEL_AUTH_KEY = "platform_raw_model"
PLATFORM_RUN_ID_AUTH_KEY = "platform_run_id"
PLATFORM_ATTRIBUTION_AUTH_KEY = "platform_attribution"

WAM_PROVIDER = "wam"


def usage_hooks():
    """`tools.usage_hooks` if the usage plugin is up, else None."""
    try:
        import tools  # pylint: disable=C0415
        return tools.usage_hooks
    except (ImportError, AttributeError):
        return None


def usage_mode(hooks):
    """MODE_OFF on any resolution failure — metering must never break the proxy."""
    if hooks is None:
        return MODE_OFF
    try:
        return hooks.usage_get_mode()
    except Exception:  # pylint: disable=W0703
        return MODE_OFF


def resolve_project_id(hooks, proxy_auth, headers):
    """The project a call is billed to. The ladder itself belongs to the usage plugin."""
    if hooks is None:
        return None
    #
    user = proxy_auth.get("user") or {}
    #
    try:
        return hooks.usage_resolve_project_id(
            user.get("id"), user.get("name"), headers, project_user_name_prefix(),
        )
    except Exception:  # pylint: disable=W0703
        log.exception("Failed to resolve the project for a metered LLM call")
        return None


def request_usage_frame(hooks, proxy_target):
    """Ask the upstream to report usage for a streamed call, on the paths that need asking."""
    if hooks is None:
        return
    #
    try:
        hooks.request_usage_frame(proxy_target)
    except Exception:  # pylint: disable=W0703
        log.exception("Failed to request a usage frame")


def project_user_name_prefix():
    """Service-user name prefix, resolved lazily so this plugin needs no init_after."""
    try:
        import tools  # pylint: disable=C0415
        return tools.project_constants["PROJECT_USER_NAME_PREFIX"]
    except (ImportError, AttributeError, KeyError, TypeError):
        log.warning("Cannot read the service user name prefix; project ladder will skip that rung")
        return None
