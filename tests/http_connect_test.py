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
from typing import TypeAlias

from absl import flags
from absl.testing import absltest
from typing_extensions import override

from framework import xds_flags
from framework import xds_k8s_testcase
from framework.infrastructure import gcp
from framework.infrastructure import k8s
from framework.infrastructure import traffic_director
from framework.test_app.runners.k8s import k8s_xds_client_runner
from framework.test_app.runners.k8s import k8s_xds_server_runner

logger = logging.getLogger(__name__)
flags.adopt_module_key_flags(xds_k8s_testcase)

_XdsTestServer = xds_k8s_testcase.XdsTestServer
_XdsTestClient = xds_k8s_testcase.XdsTestClient
KubernetesServerRunner = k8s_xds_server_runner.KubernetesServerRunner
KubernetesClientRunner: TypeAlias = k8s_xds_client_runner.KubernetesClientRunner
ClientDeploymentArgs: TypeAlias = k8s_xds_client_runner.ClientDeploymentArgs


class HttpConnectTest(xds_k8s_testcase.AppNetXdsKubernetesTestCase):
    server_runner: KubernetesServerRunner
    client_runner: KubernetesClientRunner
    server_network: str
    forward_proxy_service_attachment: str
    enable_http_connect: bool
    expected_http_connect_proxy: str
    region: str

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
        cls.enable_http_connect = xds_flags.ENABLE_HTTP_CONNECT.value
        cls.expected_http_connect_proxy = (
            xds_flags.EXPECTED_HTTP_CONNECT_PROXY.value
        )
        (
            cls.expected_proxy_ip,
            expected_proxy_port_str,
        ) = cls.expected_http_connect_proxy.split(":")
        cls.expected_proxy_port = int(expected_proxy_port_str)
        cls.region = xds_flags.HTTP_CONNECT_REGION.value

    @override
    def initTrafficDirectorManager(
        self,
    ) -> traffic_director.TrafficDirectorAppNetManager:
        td = super().initTrafficDirectorManager()

        # Override netsvc to use alpha
        td.netsvc = gcp.network_services.NetworkServicesAlpha1(
            self.gcp_api_manager, self.project
        )
        return td

    @override
    def initKubernetesServerRunner(self, **kwargs) -> KubernetesServerRunner:
        return KubernetesServerRunner(
            k8s.KubernetesNamespace(
                self.secondary_k8s_api_manager, self.server_namespace
            ),
            deployment_name=self.server_name,
            image_name=self.server_image,
            td_bootstrap_image=self.td_bootstrap_image,
            gcp_project=self.project,
            gcp_api_manager=self.gcp_api_manager,
            gcp_service_account=self.gcp_service_account,
            xds_server_uri=self.xds_server_uri,
            xds_server_region=self.xds_server_region,
            network=self.server_network,
            debug_use_port_forwarding=self.debug_use_port_forwarding,
            enable_workload_identity=self.enable_workload_identity,
            workload_identity_iam_policy_binding=self.workload_identity_iam_policy_binding,
            **kwargs,
        )

    @override
    def initKubernetesClientRunner(self, **kwargs) -> KubernetesClientRunner:
        return super().initKubernetesClientRunner(
            deployment_args=ClientDeploymentArgs(
                enable_http_connect=self.enable_http_connect,
            )
        )

    def forward_proxy_service_attachment_name(self):
        return (
            f"projects/{self.project}/regions/{self.region}/"
            f"serviceAttachments/{self.forward_proxy_service_attachment}"
        )

    def test_http_connect(self):
        with self.subTest("00_check_preprovisioned_resources"):
            service_attachment = self.td.compute.get_service_attachment(
                self.forward_proxy_service_attachment,
                region=self.region,
            )

            # 1. Confirm Server PSC Service Attachment exists and targets an SWP
            # gateway in --server_network on the expected proxy port.
            self.assertPscServiceAttachmentTargetsSwpGateway(
                service_attachment,
                network=self.server_network,
                region=self.region,
                port=self.expected_proxy_port,
            )

            # 2. Fetch Client PSC forwarding rule within --network and assert
            # its IP address matches the expected proxy IP.
            self.assertClientPscForwardingRule(
                service_attachment,
                network=self.network,
                region=self.region,
                expected_ip=self.expected_proxy_ip,
            )

        with self.subTest("01_create_health_check"):
            self.td.create_health_check()

        with self.subTest("02_create_backend_service"):
            self.td.create_backend_service(allow_multinetwork=True)

        with self.subTest("03_create_mesh"):
            body = {
                "forwardProxyServiceAttachments": [
                    self.forward_proxy_service_attachment_name(),
                ]
            }
            self.td.create_mesh(body=body)

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
            xds_config = test_client.csds.fetch_client_status_parsed(
                log_level=logging.INFO
            )

            # TODO(sreenithi): Remove before raising for review
            logger.info("CSDS DumpedXdsConfig:\n%s", xds_config)

            # Assert only 1 cluster exists as we have only one backend service
            self.assertLen(xds_config.cds, 1)

            # Verify CDS transportSocket is envoy.transport_sockets.http_11_proxy
            cluster = xds_config.cds[0]
            self.assertCdsHttpConnectTransportSocket(cluster)

            # Assert only 1 endpoint exists as we have only one backend service
            self.assertLen(xds_config.eds, 1)
            cla = xds_config.eds[0]
            self.assertLen(cla.get("endpoints", []), 1)

            endpoint = cla["endpoints"][0]
            meta = endpoint.get("metadata", {})

            self.assertEdsHttpConnectProxyAddress(
                meta, self.expected_http_connect_proxy
            )

            self.assertEdsTransportSocketNetworkType(meta)

        with self.subTest("10_assert_successful_rpcs"):
            self.assertSuccessfulRpcs(test_client)

        with self.subTest("11_assert_channelz_http_connect_address"):
            client_sock = test_client.get_active_server_channel_socket()
            remote_addr = test_client.channelz.sock_address_to_str(
                client_sock.remote
            )
            logger.info(
                "Active client socket addresses: %s",
                test_client.channelz.sock_addresses_pretty(client_sock),
            )
            self.assertEqual(remote_addr, self.expected_http_connect_proxy)


if __name__ == "__main__":
    absltest.main(failfast=True)
