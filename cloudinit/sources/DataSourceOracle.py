# This file is part of cloud-init. See LICENSE file for license information.
"""Datasource for Oracle (OCI/Oracle Cloud Infrastructure)

Notes:
 * This datasource does not support OCI Classic. OCI Classic provides an EC2
   lookalike metadata service.
 * The UUID provided in DMI data is not the same as the meta-data provided
   instance-id, but has an equivalent lifespan.
 * We do need to support upgrade from an instance that cloud-init
   identified as OpenStack.
 * Bare metal instances use iSCSI root, virtual machine instances do not.
 * Both bare metal and virtual machine instances provide a chassis-asset-tag of
   OracleCloud.com.
"""

import base64
import ipaddress
import json
import logging
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from cloudinit import atomic_helper, dmi, net, sources, util
from cloudinit.distros.networking import NetworkConfig
from cloudinit.net import (
    cmdline,
    ephemeral,
    get_interfaces_by_mac,
    is_netfail_master,
)
from cloudinit.url_helper import wait_for_url

LOG = logging.getLogger(__name__)

BUILTIN_DS_CONFIG = {
    # Don't use IMDS to configure secondary NICs by default
    "configure_secondary_nics": False,
}
CHASSIS_ASSET_TAG = "OracleCloud.com"
IPV4_METADATA_ROOT = "http://169.254.169.254/opc/v{version}/"
IPV6_METADATA_ROOT = "http://[fd00:c1::a9fe:a9fe]/opc/v{version}/"
IPV4_METADATA_PATTERN = IPV4_METADATA_ROOT + "{path}/"
IPV6_METADATA_PATTERN = IPV6_METADATA_ROOT + "{path}/"

V2_HEADERS = {"Authorization": "Bearer Oracle"}

# https://docs.cloud.oracle.com/iaas/Content/Network/Troubleshoot/connectionhang.htm#Overview,
# indicates that an MTU of 9000 is used within OCI
MTU = 9000


class ReadOpcMetadataResponse(NamedTuple):
    version: int
    instance_data: Dict[str, Any]
    vnics_data: Optional[Dict[str, Any]]
    imds_url_used: str


class KlibcOracleNetworkConfigSource(cmdline.KlibcNetworkConfigSource):
    """Override super class to lower the applicability conditions.

    If any `/run/net-*.cfg` files exist, then it is applicable. Even if
    `/run/initramfs/open-iscsi.interface` does not exist.
    """

    def is_applicable(self) -> bool:
        """Override is_applicable"""
        return bool(self._files)


def _ensure_netfailover_safe(network_config: NetworkConfig) -> None:
    """
    Search network config physical interfaces to see if any of them are
    a netfailover master.  If found, we prevent matching by MAC as the other
    failover devices have the same MAC but need to be ignored.

    Note: we rely on cloudinit.net changes which prevent netfailover devices
    from being present in the provided network config.  For more details about
    netfailover devices, refer to cloudinit.net module.

    :param network_config
       A v1 or v2 network config dict with the primary NIC, and possibly
       secondary nic configured.  This dict will be mutated.

    """
    # ignore anything that's not an actual network-config
    if "version" not in network_config:
        return

    if network_config["version"] not in [1, 2]:
        LOG.debug(
            "Ignoring unknown network config version: %s",
            network_config["version"],
        )
        return

    mac_to_name = get_interfaces_by_mac()
    if network_config["version"] == 1:
        for cfg in [c for c in network_config["config"] if "type" in c]:
            if cfg["type"] == "physical":
                if "mac_address" in cfg:
                    mac = cfg["mac_address"]
                    cur_name = mac_to_name.get(mac)
                    if not cur_name:
                        continue
                    elif is_netfail_master(cur_name):
                        del cfg["mac_address"]

    elif network_config["version"] == 2:
        for _, cfg in network_config.get("ethernets", {}).items():
            if "match" in cfg:
                macaddr = cfg.get("match", {}).get("macaddress")
                if macaddr:
                    cur_name = mac_to_name.get(macaddr)
                    if not cur_name:
                        continue
                    elif is_netfail_master(cur_name):
                        del cfg["match"]["macaddress"]
                        del cfg["set-name"]
                        cfg["match"]["name"] = cur_name


class DataSourceOracle(sources.DataSource):

    dsname = "Oracle"
    system_uuid = None
    network_config_sources: Tuple[sources.NetworkConfigSource, ...] = (
        sources.NetworkConfigSource.CMD_LINE,
        sources.NetworkConfigSource.SYSTEM_CFG,
        sources.NetworkConfigSource.DS,
        sources.NetworkConfigSource.INITRAMFS,
    )

    # for init-local stage, we want to bring up an ephemeral network
    perform_dhcp_setup = True

    # Careful...these can be overridden in __init__
    url_max_wait = 30
    url_timeout = 5

    def __init__(self, sys_cfg, *args, **kwargs):
        super(DataSourceOracle, self).__init__(sys_cfg, *args, **kwargs)
        self._vnics_data = None

        self.ds_cfg = util.mergemanydict(
            [
                util.get_cfg_by_path(sys_cfg, ["datasource", self.dsname], {}),
                BUILTIN_DS_CONFIG,
            ]
        )
        self._network_config_source = KlibcOracleNetworkConfigSource()
        self._network_config: dict = {"config": [], "version": 1}

        url_params = self.get_url_params()
        self.url_max_wait = url_params.max_wait_seconds
        self.url_timeout = url_params.timeout_seconds

    def _unpickle(self, ci_pkl_version: int) -> None:
        super()._unpickle(ci_pkl_version)
        if not hasattr(self, "_vnics_data"):
            setattr(self, "_vnics_data", None)
        if not hasattr(self, "_network_config_source"):
            setattr(
                self,
                "_network_config_source",
                KlibcOracleNetworkConfigSource(),
            )
        if not hasattr(self, "_network_config"):
            self._network_config = {"config": [], "version": 1}

    def _has_network_config(self) -> bool:
        return bool(self._network_config.get("config", []))

    @staticmethod
    def ds_detect() -> bool:
        """Check platform environment to report if this datasource may run."""
        return _is_platform_viable()

    def _get_data(self):

        self.system_uuid = _read_system_uuid()

        connectivity_urls_data = (
            {
                "url": IPV4_METADATA_PATTERN.format(
                    version=2, path="instance"
                ),
                "headers": V2_HEADERS,
            },
            {
                "url": IPV4_METADATA_PATTERN.format(
                    version=1, path="instance"
                ),
            },
            {
                "url": IPV6_METADATA_PATTERN.format(
                    version=2, path="instance"
                ),
                "headers": V2_HEADERS,
            },
            {
                "url": IPV6_METADATA_PATTERN.format(
                    version=1, path="instance"
                ),
            },
        )

        if self.perform_dhcp_setup:
            nic_name = net.find_fallback_nic()
            network_context = ephemeral.EphemeralIPNetwork(
                distro=self.distro,
                interface=nic_name,
                ipv6=True,
                ipv4=True,
                connectivity_urls_data=connectivity_urls_data,
            )
        else:
            network_context = util.nullcontext()
        fetch_primary_nic = not self._is_iscsi_root()
        fetch_secondary_nics = self.ds_cfg.get(
            "configure_secondary_nics",
            BUILTIN_DS_CONFIG["configure_secondary_nics"],
        )

        with network_context:
            fetched_metadata = read_opc_metadata(
                fetch_vnics_data=fetch_primary_nic or fetch_secondary_nics,
                max_wait=self.url_max_wait,
                timeout=self.url_timeout,
                metadata_patterns=[
                    IPV6_METADATA_PATTERN,
                    IPV4_METADATA_PATTERN,
                ],
            )

        if not fetched_metadata:
            return False
        # set the metadata root address that worked to allow for detecting
        # whether ipv4 or ipv6 was used for getting metadata
        self.metadata_address = _get_versioned_metadata_base_url(
            url=fetched_metadata.imds_url_used
        )

        data = self._crawled_metadata = fetched_metadata.instance_data
        self._vnics_data = fetched_metadata.vnics_data

        self.metadata = {
            "availability-zone": data["ociAdName"],
            "instance-id": data["id"],
            "launch-index": 0,
            "local-hostname": data["hostname"],
            "name": data["displayName"],
        }

        if "metadata" in data:
            user_data = data["metadata"].get("user_data")
            if user_data:
                self.userdata_raw = base64.b64decode(user_data)
            self.metadata["public_keys"] = data["metadata"].get(
                "ssh_authorized_keys"
            )

        return True

    def check_instance_id(self, sys_cfg) -> bool:
        """quickly check (local only) if self.instance_id is still valid

        On Oracle, the dmi-provided system uuid differs from the instance-id
        but has the same life-span."""
        return sources.instance_id_matches_system_uuid(self.system_uuid)

    def get_public_ssh_keys(self):
        return sources.normalize_pubkey_data(self.metadata.get("public_keys"))

    def _is_iscsi_root(self) -> bool:
        """Return whether we are on a iscsi machine."""
        return self._network_config_source.is_applicable()

    def _get_iscsi_config(self) -> dict:
        return self._network_config_source.render_config()

    @property
    def network_config(self):
        """Network config is read from initramfs provided files

        Priority for primary network_config selection:
        - iscsi
        - imds

        If none is present, then we fall back to fallback configuration.
        """
        if self._has_network_config():
            return self._network_config

        set_primary = False
        if self._is_iscsi_root():
            self._network_config = self._get_iscsi_config()
            logging.debug(
                "Instance is using iSCSI root, setting primary NIC as critical"
            )
            # This is necessary for Oracle baremetal instances in case they are
            # running on an IPv6-only network. Without this, they become
            # unreachable/unrecoverable after a shutdown.
            self._network_config["config"][0]["keep_configuration"] = True
        if not self._has_network_config():
            LOG.debug(
                "Could not obtain network configuration from initramfs. "
                "Falling back to IMDS."
            )
            set_primary = True

        set_secondary = self.ds_cfg.get(
            "configure_secondary_nics",
            BUILTIN_DS_CONFIG["configure_secondary_nics"],
        )
        if set_primary or set_secondary:
            try:
                # Mutate self._network_config to include primary and/or
                # secondary VNICs
                self._add_network_config_from_opc_imds(set_primary)
            except Exception:
                util.logexc(
                    LOG,
                    "Failed to parse IMDS network configuration!",
                )

        # we need to verify that the nic selected is not a netfail over
        # device and, if it is a netfail master, then we need to avoid
        # emitting any match by mac
        _ensure_netfailover_safe(self._network_config)

        return self._network_config

    def _add_network_config_from_opc_imds(self, set_primary: bool = False):
        """Generate primary and/or secondary NIC config from IMDS and merge it.

        It will mutate the network config to include the secondary VNICs.

        :param set_primary: If True set primary interface.
        :raises:
        Exceptions are not handled within this function.  Likely
            exceptions are KeyError/IndexError
            (if the IMDS returns valid JSON with unexpected contents).
        """
        if self._vnics_data is None:
            LOG.warning("NIC data is UNSET but should not be")
            return

        if not set_primary and ("nicIndex" in self._vnics_data[0]):
            # TODO: Once configure_secondary_nics defaults to True, lower the
            # level of this log message.  (Currently, if we're running this
            # code at all, someone has explicitly opted-in to secondary
            # VNIC configuration, so we should warn them that it didn't
            # happen.  Once it's default, this would be emitted on every Bare
            # Metal Machine launch, which means INFO or DEBUG would be more
            # appropriate.)
            LOG.warning(
                "VNIC metadata indicates this is a bare metal machine; "
                "skipping secondary VNIC configuration."
            )
            return

        interfaces_by_mac = get_interfaces_by_mac()

        vnics_data = self._vnics_data if set_primary else self._vnics_data[1:]

        # If the metadata address is an IPv6 address

        for index, vnic_dict in enumerate(vnics_data):
            is_primary = set_primary and index == 0
            mac_address = vnic_dict["macAddr"].lower()
            is_ipv6_only = vnic_dict.get(
                "ipv6VirtualRouterIp", False
            ) and not vnic_dict.get("privateIp", False)
            if mac_address not in interfaces_by_mac:
                LOG.warning(
                    "Interface with MAC %s not found; skipping",
                    mac_address,
                )
                continue
            name = interfaces_by_mac[mac_address]
            if is_ipv6_only:
                network = ipaddress.ip_network(
                    vnic_dict["ipv6Addresses"][0],
                )
            else:
                network = ipaddress.ip_network(vnic_dict["subnetCidrBlock"])

            if is_primary:
                if is_ipv6_only:
                    subnets = [{"type": "dhcp6"}]
                else:
                    subnets = [{"type": "dhcp"}]
            else:
                subnets = []
                if vnic_dict.get("privateIp"):
                    subnets.append(
                        {
                            "type": "static",
                            "address": (
                                f"{vnic_dict['privateIp']}/"
                                f"{network.prefixlen}"
                            ),
                        }
                    )
                if vnic_dict.get("ipv6Addresses"):
                    subnets.append(
                        {
                            "type": "static",
                            "address": (
                                f"{vnic_dict['ipv6Addresses'][0]}/"
                                f"{network.prefixlen}"
                            ),
                        }
                    )
            interface_config = {
                "name": name,
                "type": "physical",
                "mac_address": mac_address,
                "mtu": MTU,
                "subnets": subnets,
            }
            self._network_config["config"].append(interface_config)


class DataSourceOracleNet(DataSourceOracle):
    perform_dhcp_setup = False


def _is_ipv4_metadata_url(metadata_address: str):
    if not metadata_address:
        return False
    return metadata_address.startswith(IPV4_METADATA_ROOT.split("opc")[0])


def _read_system_uuid() -> Optional[str]:
    sys_uuid = dmi.read_dmi_data("system-uuid")
    return None if sys_uuid is None else sys_uuid.lower()


def _is_platform_viable() -> bool:
    asset_tag = dmi.read_dmi_data("chassis-asset-tag")
    return asset_tag == CHASSIS_ASSET_TAG


def _url_version(url: str) -> int:
    return 2 if "/opc/v2/" in url else 1


def _headers_cb(url: str) -> Optional[Dict[str, str]]:
    return V2_HEADERS if _url_version(url) == 2 else None


def _get_versioned_metadata_base_url(url: str) -> str:
    """
    Remove everything following the version number in the metadata address.
    """
    if not url:
        return url
    if "v2" in url:
        return url.split("v2")[0] + "v2/"
    elif "v1" in url:
        return url.split("v1")[0] + "v1/"
    else:
        raise ValueError("Invalid metadata address: " + url)


def read_opc_metadata(
    *,
    fetch_vnics_data: bool = False,
    max_wait=DataSourceOracle.url_max_wait,
    timeout=DataSourceOracle.url_timeout,
    metadata_patterns: List[str] = [IPV4_METADATA_PATTERN],
) -> Optional[ReadOpcMetadataResponse]:
    """
    Fetch metadata from the /opc/ routes from the IMDS.

    Returns:
        Optional[ReadOpcMetadataResponse]: If fetching metadata fails, None.
            If fetching metadata succeeds, a namedtuple containing:
            - The metadata version as an integer
            - The JSON-decoded value of the instance data from the IMDS
            - The JSON-decoded value of the vnics data from the IMDS if
                `fetch_vnics_data` is True, else None. Alternatively,
                None if fetching metadata failed
            - The url that was used to fetch the metadata.
                This allows for later determining if v1 or v2 endppoint was
                used and whether the IMDS was reached via IPv4 or IPv6.
    """
    urls = [
        metadata_pattern.format(version=version, path="instance")
        for version in [2, 1]
        for metadata_pattern in metadata_patterns
    ]

    LOG.debug("Attempting to fetch IMDS metadata from: %s", urls)
    start_time = time.monotonic()
    url_that_worked, instance_response = wait_for_url(
        urls=urls,
        max_wait=max_wait,
        timeout=timeout,
        headers_cb=_headers_cb,
        sleep_time=0.1,
        connect_synchronously=True,
    )
    if not url_that_worked:
        LOG.warning(
            "Failed to fetch IMDS metadata from any of: %s",
            ", ".join(urls),
        )
        return None
    else:
        LOG.debug(
            "Successfully fetched instance metadata from IMDS at: %s",
            url_that_worked,
        )
    instance_data = json.loads(instance_response.decode("utf-8"))

    # save whichever version we got the instance data from for vnics data later
    metadata_version = _url_version(url_that_worked)

    vnics_data = None
    if fetch_vnics_data:
        # This allows us to go over the max_wait time by the timeout length,
        # but if we were able to retrieve instance metadata, that seems
        # like a worthwhile tradeoff rather than having incomplete metadata.
        vnics_url, vnics_response = wait_for_url(
            urls=[url_that_worked.replace("instance", "vnics")],
            max_wait=max_wait - (time.monotonic() - start_time),
            timeout=timeout,
            headers_cb=_headers_cb,
            sleep_time=0.1,
            connect_synchronously=True,
        )
        if vnics_url:
            vnics_data = json.loads(vnics_response.decode("utf-8"))
            LOG.debug(
                "Successfully fetched vnics metadata from IMDS at: %s",
                vnics_url,
            )
        else:
            LOG.warning("Failed to fetch IMDS network configuration!")
    return ReadOpcMetadataResponse(
        metadata_version,
        instance_data,
        vnics_data,
        url_that_worked,
    )


# Used to match classes to dependencies
datasources = [
    (DataSourceOracle, (sources.DEP_FILESYSTEM,)),
    (
        DataSourceOracleNet,
        (
            sources.DEP_FILESYSTEM,
            sources.DEP_NETWORK,
        ),
    ),
]


# Return a list of data sources that match this set of dependencies
def get_datasource_list(depends):
    return sources.list_from_depends(depends, datasources)


if __name__ == "__main__":

    description = """
        Query Oracle Cloud metadata and emit a JSON object with two keys:
        `read_opc_metadata` and `_is_platform_viable`.  The values of each are
        the return values of the corresponding functions defined in
        DataSourceOracle.py."""

    print(
        atomic_helper.json_dumps(
            {
                "read_opc_metadata": read_opc_metadata(
                    metadata_patterns=(
                        [IPV6_METADATA_PATTERN, IPV4_METADATA_PATTERN]
                    )
                ),
                "_is_platform_viable": _is_platform_viable(),
            }
        )
    )
