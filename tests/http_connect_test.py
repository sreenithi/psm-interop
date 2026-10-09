# Copyright 2026 gRPC authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

from absl import flags
from absl.testing import absltest

from framework import xds_flags
from framework import xds_k8s_testcase

logger = logging.getLogger(__name__)
flags.adopt_module_key_flags(xds_k8s_testcase)

_XdsTestServer = xds_k8s_testcase.XdsTestServer
_XdsTestClient = xds_k8s_testcase.XdsTestClient

class HttpConnectTest(xds_k8s_testcase.AppNetXdsKubernetesTestCase):

    server_network: str
    forward_proxy_service_attachment: str

    @classmethod
    def setUpClass(cls):
        """Hook method for setting up class fixture before running tests in
        the class.
        """
        super().setUpClass()
        cls.server_network = xds_flags.SERVER_NETWORK.value
        cls.forward_proxy_service_attachment = (
            xds_flags.FORWARD_PROXY_SERVICE_ATTACHMENT.value
        )

    def forward_proxy_service_attachment_name(self):
        return (
            f"projects/{self.project}/regions/{self.region}/"
            f"serviceAttachments/{self.forward_proxy_service_attachment}"
        )

    def test_http_connect(self):
        with self.subTest("00_check_preprovisioned_resources"):
            pass

        with self.subTest("01_create_health_check"):
            self.td.create_health_check()

        with self.subTest("02_create_backend_service"):
            self.td.create_backend_service()

        with self.subTest("03_create_mesh"):
            self.td.create_mesh()

        with self.subTest("04_create_grpc_route"):
            self.td.create_grpc_route(
                self.server_xds_host, self.server_xds_port
            )

        test_server: _XdsTestServer
        with self.subTest("05_start_test_server"):
            test_server = self.startTestServers(replica_count=1)[0]

        with self.subTest("06_setup_server_backends"):
            self.setupServerBackends()

        test_client: _XdsTestClient
        with self.subTest("07_start_test_client"):
            test_client = self.startTestClient(
                test_server, config_mesh=self.td.mesh.name
            )

        with self.subTest("08_assert_xds_config_exists"):
            self.assertXdsConfigExists(test_client)

        with self.subTest("09_assert_xds_http_connect_config"):
            pass

        with self.subTest("10_assert_successful_rpcs"):
            self.assertSuccessfulRpcs(test_client)

        with self.subTest("11_assert_channelz_http_connect_address"):
            pass

if __name__ == "__main__":
    absltest.main(failfast=True)
