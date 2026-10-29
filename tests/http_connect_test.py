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
from framework.test_app.runners.k8s import k8s_xds_server_runner
from framework.test_app.runners.k8s import k8s_xds_client_runner

logger = logging.getLogger(__name__)
flags.adopt_module_key_flags(xds_k8s_testcase)

_XdsTestServer = xds_k8s_testcase.XdsTestServer
_XdsTestClient = xds_k8s_testcase.XdsTestClient
KubernetesServerRunner = k8s_xds_server_runner.KubernetesServerRunner
KubernetesClientRunner: TypeAlias = k8s_xds_server_runner.KubernetesClientRunner
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

    def _assert_psc_sa_target_gateway(self, service_attachment):
        target_service = service_attachment["targetService"]
        gateway_name = target_service.rsplit("/", 1)[-1]

        gateway = self.td.netsvc.get_gateway(gateway_name, location=self.region)
        self.assertTrue(
            gateway["network"].endswith(f"/networks/{self.server_network}"),
            f"Expected SWP gateway network to be {self.server_network}, "
            f"got {gateway['network']}",
        )
        self.assertEqual(gateway["ports"], [self.expected_proxy_port])
        logger.info(
            "Verified the PSC Service attachment %s exists within %s "
            "on port %d",
            self.forward_proxy_service_attachment,
            self.server_network,
            self.expected_proxy_port,
        )

    def _assert_client_psc_forwarding_rule(self, service_attachment):
        connected_endpoints = [
            ep
            for ep in service_attachment.get("connectedEndpoints", [])
            if ep.get("consumerNetwork", "").endswith(
                f"/networks/{self.network}"
            )
        ]

        self.assertLen(
            connected_endpoints,
            1,
            f"Expected 1 connected endpoint in {self.network} on "
            f"service attachment {self.forward_proxy_service_attachment}, "
            f"got {service_attachment.get('connectedEndpoints')}",
        )
        connected_ep = connected_endpoints[0]

        # Verify PSC connection readiness on both data and control planes:
        # - pscConnectionId is used by Traffic Director to map the
        #   ServiceAttachment to the client PSC forwarding rule and populate
        #   the proxy address in EDS.
        # - status == "ACCEPTED" ensures the PSC tunnel can forward traffic
        #   (pscConnectionId is set even if status is PENDING or REJECTED).
        self.assertEqual(connected_ep.get("status"), "ACCEPTED")
        self.assertIn("pscConnectionId", connected_ep)

        fw_rule_name = connected_ep["endpoint"].rsplit("/", 1)[-1]

        forwarding_rule = self.td.compute.get_forwarding_rule(
            fw_rule_name, region=self.region
        )

        self.assertTrue(
            forwarding_rule["network"].endswith(f"/networks/{self.network}"),
            f"Expected forwarding rule network to be {self.network}, "
            f"got {forwarding_rule['network']}",
        )

        self.assertEqual(forwarding_rule["IPAddress"], self.expected_proxy_ip)

    def test_http_connect(self):
        with self.subTest("00_check_preprovisioned_resources"):
            service_attachment = self.td.compute.get_service_attachment(
                self.forward_proxy_service_attachment,
                region=self.region,
            )

            # 1. Confirm Server PSC Service Attachment exists and targets an SWP
            # gateway in --server_network on the expected proxy port.
            self._assert_psc_sa_target_gateway(service_attachment)

            # 2. Fetch Client PSC forwarding rule within --network and assert
            # its IP address matches the expected proxy IP.
            self._assert_client_psc_forwarding_rule(service_attachment)

        with self.subTest("01_create_health_check"):
            self.td.create_health_check()

        with self.subTest("02_create_backend_service"):
            self.td.create_backend_service()

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
            pass

        with self.subTest("10_assert_successful_rpcs"):
            self.assertSuccessfulRpcs(test_client)

        with self.subTest("11_assert_channelz_http_connect_address"):
            pass

if __name__ == "__main__":
    absltest.main(failfast=True)
