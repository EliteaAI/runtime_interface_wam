#!/usr/bin/python3
# coding=utf-8

#   Copyright 2025 EPAM Systems
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

""" Route """

import flask  # pylint: disable=E0401

from pylon.core.tools import log  # pylint: disable=E0611,E0401,W0611
from pylon.core.tools import web  # pylint: disable=E0611,E0401,W0611

from tools import this, auth  # pylint: disable=E0401

from ..utils.metering import (
    MODE_OFF,
    PLATFORM_ATTRIBUTION_AUTH_KEY,
    PLATFORM_PROJECT_ID_AUTH_KEY,
    PLATFORM_RAW_MODEL_AUTH_KEY,
    PLATFORM_RUN_ID_AUTH_KEY,
    WAM_PROVIDER,
    usage_hooks,
    usage_mode,
)


def metered_iterator(proxy_target, proxy_auth, response, iterator):
    """The iterator to serve, metered when the usage plugin is up and asking for it.

    Only a call prepare_request marked billable is metered, so a path that never reaches the
    upstream — /v1/models, a rejected request — cannot produce a row.

    X-Elitea-Audited is deliberately not consulted: whether a call is billable is the usage
    plugin's decision, never a caller's header. The header suppresses the legacy audit-trail
    span, which this path never emitted.
    """
    hooks = usage_hooks()
    #
    if PLATFORM_RAW_MODEL_AUTH_KEY not in proxy_auth or usage_mode(hooks) == MODE_OFF:
        return iterator
    #
    try:
        usage_context = hooks.begin_llm_call(
            project_id=proxy_auth.get(PLATFORM_PROJECT_ID_AUTH_KEY),
            user_id=(proxy_auth.get("user") or {}).get("id"),
            model_name=proxy_auth.get(PLATFORM_RAW_MODEL_AUTH_KEY),
            endpoint=proxy_target["endpoint"],
            headers=flask.request.headers,
            provider=WAM_PROVIDER,
            run_id=proxy_auth.get(PLATFORM_RUN_ID_AUTH_KEY),
            attribution=proxy_auth.get(PLATFORM_ATTRIBUTION_AUTH_KEY),
        )
        #
        return hooks.meter_llm_response(usage_context, response, iterator)
    except:  # pylint: disable=W0702
        log.exception("Failed to meter LLM call")
        return iterator


class Route:  # pylint: disable=E1101,R0903
    """ Route """

    @web.route(
        "/",
        defaults={"url": "/"},
        methods=["OPTIONS", "HEAD", "GET", "POST", "PUT", "PATCH", "DELETE"],
        endpoint="wam_route_http",
    )
    @web.route(
        "/<path:url>",
        methods=["OPTIONS", "HEAD", "GET", "POST", "PUT", "PATCH", "DELETE"],
        endpoint="wam_route_http__url",
    )
    def wam_route_http(self, url):  # pylint: disable=R
        """ Handler """
        #
        # Target
        #
        proxy_target = {
            "url": url,
            "endpoint": f'/{url.lstrip("/")}',
            #
            "method": flask.request.method,
            "params": dict(flask.request.args),
            "headers": flask.request.headers,
            #
            **self.preprocess_data(flask.request),
        }
        #
        # Auth
        #
        proxy_auth = {
            "type": flask.g.auth.type,
            "user": auth.current_user(),
        }
        #
        # Check auth
        #
        auth_check_response = self.check_access(proxy_target, proxy_auth)
        #
        if auth_check_response is not None:
            return auth_check_response
        #
        # Prepare request
        #
        prepare_request_response = self.prepare_request(proxy_target, proxy_auth)
        #
        if prepare_request_response is not None:
            return prepare_request_response
        #
        # Perform request
        #
        response_stream_id = self.stream_node.add_stream()
        #
        try:
            request_stream_id = self.service_node.call.wam_request_start(
                response_stream_id, this.descriptor.config.copy(),
            )
        except Exception as exc:  # pylint: disable=W0703
            self.stream_node.remove_stream(response_stream_id)
            #
            log.exception("Proxy exception")
            return {"error": {"message": str(exc)}}, 500
        #
        emitter = None
        #
        try:
            emitter = self.stream_node.get_emitter(request_stream_id)
            consumer = self.stream_node.get_consumer(
                response_stream_id,
                timeout=this.descriptor.config.get("proxy_consumer_timeout", 600),
            )
            iterator = iter(consumer)
            #
            emitter.chunk(proxy_target)
            #
            response = next(iterator)
            #
            self.prepare_response(proxy_target, proxy_auth, response)
            #
            iterator = metered_iterator(proxy_target, proxy_auth, response, iterator)
            #
            return flask.Response(
                flask.stream_with_context(iterator),
                status=response["status_code"],
                headers=response["headers"],
                direct_passthrough=True,
            )
        #
        except Exception as exc:  # pylint: disable=W0703
            log.exception("Proxy exception")
            return {"error": {"message": str(exc)}}, 500
        #
        finally:
            if emitter is not None:
                emitter.end()
