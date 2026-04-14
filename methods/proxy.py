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

""" Method """

from pylon.core.tools import log  # pylint: disable=E0611,E0401,W0611
from pylon.core.tools import web  # pylint: disable=E0611,E0401,W0611

from werkzeug.datastructures.headers import Headers  # pylint: disable=E0401

from tools import context  # pylint: disable=E0401


class Method:  # pylint: disable=E1101,R0903,W0201
    """
        Method Resource

        self is pointing to current Module instance

        web.method decorator takes zero or one argument: method name
        Note: web.method decorator must be the last decorator (at top)
    """

    @web.method()
    def preprocess_headers(self, raw_headers):
        """ Method """
        exclude_headers = {
            "Connection",
            "Keep-Alive",
            "Proxy-Authenticate",
            "Proxy-Authorization",
            "TE",
            "Trailers",
            "Transfer-Encoding",
            "Upgrade",
        }
        #
        headers = Headers(dict(raw_headers))
        #
        for header in exclude_headers:
            headers.remove(header)
        #
        return headers

    @web.method()
    def preprocess_data(self, raw_request):
        """ Method """
        data = None
        json = None
        files = None
        #
        if raw_request.method in ["POST", "PUT", "PATCH"]:
            if raw_request.files:
                files = {
                    key: (file.filename, file.stream, file.content_type)
                    for key, file in raw_request.files.items()
                }
                data = raw_request.form
            elif raw_request.content_type == "application/json":
                json = raw_request.get_json(silent=True)
                if json is None:
                    data = raw_request.data
            elif raw_request.content_type == "application/x-www-form-urlencoded":
                data = raw_request.form
            else:
                data = raw_request.data
        #
        return {
            "data": data,
            "json": json,
            "files": files,
        }

    @web.method()
    def check_access(self, proxy_target, proxy_auth):
        """ Method """
        #
        # Whitelist
        #
        endpoint_whitelist = [
            "/v1/models",
            "/v1/completions",
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/messages",
            "/v1/embeddings",
            "/v1/images/generations",
            "/v1/images/edits",
            "/v1/images/variations",
        ]
        #
        endpoint_prefix_whitelist = [
            "/v1/models/",
            "/v1/chat/completions/",
            "/v1/responses/",
            "/v1/messages/",
        ]
        #
        target_endpoint = proxy_target["endpoint"]
        target_whitelisted = target_endpoint in endpoint_whitelist
        #
        if not target_whitelisted:
            for target_prefix in endpoint_prefix_whitelist:
                if target_endpoint.startswith(target_prefix):
                    target_whitelisted = True
                    break
        #
        if target_whitelisted:
            return None
        #
        # Check if user is admin
        #
        user_id = proxy_auth["user"]["id"]
        #
        user_administration_roles = context.rpc_manager.timeout(30).auth_get_user_roles(
            user_id, "administration",
        )
        #
        user_is_administration_admin = "admin" in user_administration_roles
        #
        # Admin can access all targets
        #
        if user_is_administration_admin:
            return None
        #
        return "Forbidden", 403

    @web.method()
    def prepare_request(self, proxy_target, proxy_auth):  # pylint: disable=R0912,R0914
        """ Method """
        _ = proxy_auth
        #
        proxy_target["headers"] = self.preprocess_headers(proxy_target["headers"])
        proxy_target["headers"]["Accept-Encoding"] = "identity"
        #
        proxy_target_endpoint = proxy_target["endpoint"]
        #
        if proxy_target_endpoint.startswith("/v1/models"):
            result = {
                "data": [],
                "object": "list",
            }
            #
            endpoint_parts = proxy_target_endpoint.strip("/").split("/", 2)
            target_model_name = None
            #
            if len(endpoint_parts) > 2:
                target_model_name = endpoint_parts[-1]
            #
            # TODO: forward to models endpoint
            #
            if target_model_name is not None:
                return "Error", 404
            #
            return result
        #
        proxy_target["headers"].remove("Authorization")
        #
        # TODO: wam_appid
        #
        model_name = None
        #
        if isinstance(proxy_target["json"], dict) and "model" in proxy_target["json"]:
            model_name = proxy_target["json"].pop("model")
        #
        if model_name is None:
            return "Error", 400
        #
        proxy_target["headers"].remove("Host")
        #
        proxy_target["url"] = proxy_target_endpoint.replace(
            "/v1/", f"/deployments/{model_name}/", 1
        )
        #
        return None

    @web.method()
    def prepare_response(self, proxy_target, proxy_auth, response):
        """ Method """
        _ = proxy_auth
        #
        response["headers"] = self.preprocess_headers(response["headers"])
        #
        if "Host" in proxy_target["headers"]:
            response["headers"]["Host"] = proxy_target["headers"]["Host"]
        else:
            response["headers"].remove("Host")
        #
        if response["headers"].get("Transfer-Encoding", "").lower() == "chunked":
            response["headers"].remove("Content-Length")
        #
        # Optimize for streaming (e.g., SSE)
        content_type = response["headers"].get("Content-Type", "")
        if content_type.startswith("text/event-stream"):
            response["headers"]["Cache-Control"] = "no-cache"
            # Disable proxy buffering for Nginx if present
            response["headers"]["X-Accel-Buffering"] = "no"
        #
        response["headers"]["Server"] = "EliteA"
