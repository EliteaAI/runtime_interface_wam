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

import arbiter  # pylint: disable=E0401

from tools import context, worker_client  # pylint: disable=E0401


class Method:  # pylint: disable=E1101,R0903,W0201
    """
        Method Resource

        self is pointing to current Module instance

        web.method decorator takes zero or one argument: method name
        Note: web.method decorator must be the last decorator (at top)
    """

    @web.init()
    def init(self):
        """ Init """
        #
        # Stream/Service nodes
        #
        self.stream_node = arbiter.StreamNode(  # pylint: disable=I1101
            worker_client.event_node,
            id_prefix="wam:",
        )
        self.service_node = arbiter.ServiceNode(  # pylint: disable=I1101
            worker_client.event_node,
            id_prefix="wam:",
            default_timeout=30,
        )
        #
        self.stream_node.start()
        self.service_node.start()
        #
        # Register configurations
        #
        # pylint: disable=C0415
        try:
            from ..models.pd.configuration.wam import WamCredential
            #
            for name, model in [
                    ("wam", WamCredential),
            ]:
                context.rpc_manager.timeout(5).configurations_register(
                    type_name=name,
                    section="ai_credentials",
                    model=model,
                )
        except:  # pylint: disable=W0702
            log.exception("Failed to register configurations")

    @web.deinit()
    def deinit(self):
        """ De-init """
        self.service_node.stop()
        self.stream_node.stop()
