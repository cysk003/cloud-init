# Copyright (C) 2021-2025 VMware by Broadcom.
#
# Author: Shreenidhi Shedi <yesshedi@gmail.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

import logging
from typing import Any, Dict, List, Optional

from cloudinit import subp, util
from cloudinit.net import renderer, should_add_gateway_onlink_flag
from cloudinit.net.network_state import NetworkState

LOG = logging.getLogger(__name__)


def normalize(data: Dict[str, List[Any]]) -> Dict[str, List[Any]]:
    """
    Normalize a dictionary of lists.
    - Assumes top-level keys map to lists.
    - Each list and any nested dicts/lists will be recursively normalized.
    """
    normalized = {}
    for key, value in data.items():
        normalized[key] = _normalize_value(value)
    return normalized


def _normalize_value(data: Any) -> Any:
    """
    Recursively normalize a dictionary or list:
    - Dicts: keys sorted, values normalized
    - Lists: items normalized and sorted (if comparable)
    """
    if isinstance(data, dict):
        normalized = {}
        for key in sorted(data):
            normalized[key] = _normalize_value(data[key])
        return normalized
    elif isinstance(data, list):
        normalized_items = []
        for item in data:
            if isinstance(item, (dict, list)):
                normalized_item = _normalize_value(item)
            else:
                normalized_item = item
            normalized_items.append(normalized_item)
        try:
            return sorted(normalized_items)
        except TypeError:
            return normalized_items

    return data


class CfgParser:
    def __init__(self):
        self.conf_dict = {
            "Match": [],
            "Link": [],
            "Network": [],
            "DHCPv4": [],
            "DHCPv6": [],
            "Address": [],
            "Route": {},
            "NetDev": [],
            "VLAN": [],
            "Bond": [],
        }

    def update_section(self, sec, key, val):
        for k in self.conf_dict.keys():
            if k == sec:
                self.conf_dict[k].append(f"{key}={val}")
                self.conf_dict[k] = list(dict.fromkeys(self.conf_dict[k]))
                self.conf_dict[k].sort()

    def update_route_section(self, sec, rid, key, val):
        """
        For each route section we use rid as a key, this allows us to isolate
        this route from others on subsequent calls.
        """
        for k in self.conf_dict.keys():
            if k == sec:
                if rid not in self.conf_dict[k]:
                    self.conf_dict[k][rid] = []
                self.conf_dict[k][rid].append(f"{key}={val}")
                # remove duplicates from list
                self.conf_dict[k][rid] = list(
                    dict.fromkeys(self.conf_dict[k][rid])
                )
                self.conf_dict[k][rid].sort()

    def get_final_conf(self):
        contents = ""

        self.conf_dict = normalize(self.conf_dict)

        for k, v in sorted(self.conf_dict.items()):
            if not v:
                continue
            if k == "Address":
                for e in sorted(v):
                    contents += f"[{k}]\n{e}\n\n"
            elif k == "Route":
                for n in sorted(v):
                    contents += f"[{k}]\n"
                    for e in sorted(v[n]):
                        contents += f"{e}\n"
                    contents += "\n"
            else:
                contents += f"[{k}]\n"
                for e in sorted(v):
                    contents += f"{e}\n"
                contents += "\n"

        return contents


class Renderer(renderer.Renderer):
    """
    Renders network information in /etc/systemd/network

    This Renderer is currently experimental and doesn't support all the
    use cases supported by the other renderers yet.
    """

    def __init__(self, config=None):
        if not config:
            config = {}
        self.resolve_conf_fn = config.get(
            "resolve_conf_fn", "/etc/systemd/resolved.conf"
        )
        self.network_conf_dir = config.get(
            "network_conf_dir", "/etc/systemd/network/"
        )

    def generate_match_section(self, iface, cfg: CfgParser):
        sec = "Match"
        match_dict = {
            "name": "Name",
            "driver": "Driver",
        }

        if iface["type"] == "physical":
            match_dict["mac_address"] = "MACAddress"

        if not iface:
            return

        for k, v in match_dict.items():
            if k in iface and iface[k]:
                cfg.update_section(sec, v, iface[k])

        return iface["name"]

    def generate_link_section(self, iface, cfg: CfgParser):
        sec = "Link"

        if not iface:
            return

        if iface.get("mtu"):
            cfg.update_section(sec, "MTUBytes", iface["mtu"])

        if iface["type"] != "physical" and iface.get("mac_address"):
            cfg.update_section(sec, "MACAddress", iface["mac_address"])

        if "optional" in iface and iface["optional"]:
            cfg.update_section(sec, "RequiredForOnline", "no")

    def parse_routes(self, rid, conf, cfg: CfgParser):
        """
        Parse a route and use rid as a key in order to isolate the route from
        others in the route dict.
        """
        sec = "Route"
        route_cfg_map = {
            "gateway": "Gateway",
            "network": "Destination",
            "metric": "Metric",
        }

        # prefix is derived using netmask by network_state
        prefix = ""
        if "prefix" in conf:
            prefix = f"/{conf['prefix']}"

        for k, v in conf.items():
            if k not in route_cfg_map:
                continue
            if k == "network":
                v += prefix
            cfg.update_route_section(sec, rid, route_cfg_map[k], v)

    def parse_subnets(self, iface, cfg: CfgParser):
        dhcp = "no"
        sec = "Network"
        rid = 0
        for e in iface.get("subnets", []):
            t = e["type"]
            if t in {"dhcp4", "dhcp"}:
                if dhcp == "no":
                    dhcp = "ipv4"
                elif dhcp == "ipv6":
                    dhcp = "yes"
            elif t == "dhcp6":
                if dhcp == "no":
                    dhcp = "ipv6"
                elif dhcp == "ipv4":
                    dhcp = "yes"
            if "routes" in e and e["routes"]:
                for i in e["routes"]:
                    # Use "r" as a dict key prefix for this route to isolate
                    # it from other sources of routes
                    self.parse_routes(f"r{rid}", i, cfg)
                    rid = rid + 1
            if "address" in e:
                addr = e["address"]
                if "prefix" in e:
                    addr += f"/{e['prefix']}"
                subnet_cfg_map = {
                    "address": "Address",
                    "gateway": "Gateway",
                    "dns_nameservers": "DNS",
                    "dns_search": "Domains",
                }
                for k, v in e.items():
                    if k == "address":
                        cfg.update_section("Address", subnet_cfg_map[k], addr)
                    elif k == "gateway":
                        # Use "a" as a dict key prefix for this route to
                        # isolate it from other sources of routes
                        cfg.update_route_section(
                            "Route", f"a{rid}", subnet_cfg_map[k], v
                        )
                        if should_add_gateway_onlink_flag(v, addr):
                            LOG.debug(
                                "Gateway %s is not contained within subnet %s,"
                                " adding GatewayOnLink flag",
                                v,
                                addr,
                            )
                            cfg.update_route_section(
                                "Route", f"a{rid}", "GatewayOnLink", "yes"
                            )
                        rid = rid + 1
                    elif k in {"dns_nameservers", "dns_search"}:
                        cfg.update_section(sec, subnet_cfg_map[k], " ".join(v))

        cfg.update_section(sec, "DHCP", dhcp)

        if isinstance(iface.get("accept-ra"), bool):
            val = "no"
            if iface["accept-ra"]:
                val = "yes"
            cfg.update_section(sec, "IPv6AcceptRA", val)

        return dhcp

    # This is to accommodate extra keys present in VMware config
    def dhcp_domain(self, d, cfg: CfgParser):
        for item in ["dhcp4domain", "dhcp6domain"]:
            if item not in d:
                continue
            ret = str(d[item]).casefold()
            try:
                ret = util.translate_bool(ret)
                ret = "yes" if ret else "no"
            except ValueError:
                if ret != "route":
                    LOG.warning("Invalid dhcp4domain value - %s", ret)
                    ret = "no"
            if item == "dhcp4domain":
                section = "DHCPv4"
            else:
                section = "DHCPv6"
            cfg.update_section(section, "UseDomains", ret)

    def parse_dns(self, iface, cfg: CfgParser, ns: NetworkState):
        sec = "Network"

        dns = iface.get("dns")
        if not dns and ns.version == 1:
            dns = {
                "search": ns.dns_searchdomains,
                "nameservers": ns.dns_nameservers,
            }
        elif not dns and ns.version == 2:
            return

        if dns.get("search"):
            cfg.update_section(sec, "Domains", " ".join(dns["search"]))
        if dns.get("nameservers"):
            cfg.update_section(sec, "DNS", " ".join(dns["nameservers"]))

    def parse_dhcp_overrides(self, cfg: CfgParser, device, dhcp, version):
        dhcp_config_maps = {
            "UseDNS": "use-dns",
            "UseDomains": "use-domains",
            "UseHostname": "use-hostname",
            "UseNTP": "use-ntp",
        }

        if version == "4":
            dhcp_config_maps.update(
                {
                    "SendHostname": "send-hostname",
                    "Hostname": "hostname",
                    "RouteMetric": "route-metric",
                    "UseMTU": "use-mtu",
                    "UseRoutes": "use-routes",
                }
            )

        if f"dhcp{version}-overrides" in device and dhcp in [
            "yes",
            f"ipv{version}",
        ]:
            dhcp_overrides = device[f"dhcp{version}-overrides"]
            for k, v in dhcp_config_maps.items():
                if v in dhcp_overrides:
                    cfg.update_section(f"DHCPv{version}", k, dhcp_overrides[v])

    def create_network_file(self, link, conf, nwk_dir, ext=".network"):
        net_fn_owner = "systemd-network"

        LOG.debug("Setting Networking Config for %s", link)
        net_fn = f"{nwk_dir}10-cloud-init-{link}{ext}"

        if conf.endswith("\n\n"):
            conf = conf[:-1]
        util.write_file(net_fn, conf)
        util.chownbyname(net_fn, net_fn_owner, net_fn_owner)

    def render_network_state(
        self,
        network_state: NetworkState,
        templates: Optional[dict] = None,
        target=None,
    ) -> None:
        network_dir = self.network_conf_dir
        if target:
            network_dir = subp.target_path(target) + network_dir

        util.ensure_dir(network_dir)

        network = self._render_content(network_state)
        vlan_netdev = network.pop("vlan_netdev", {})
        bond_netdev = network.pop("bond_netdev", {})

        for k, v in network.items():
            self.create_network_file(k, v, network_dir)

        for k, v in vlan_netdev.items():
            self.create_network_file(k, v, network_dir, ext=".netdev")

        for k, v in bond_netdev.items():
            self.create_network_file(k, v, network_dir, ext=".netdev")

    def _render_content(self, ns: NetworkState):
        ret_dict = {}
        vlan_link = {}
        bond_link = {}

        if "vlans" in ns.config:
            vlan_dict = self.render_vlans(ns)
            vlan_netdev = vlan_dict["vlan_netdev"]
            vlan_link = vlan_dict["vlan_link"]
            ret_dict["vlan_netdev"] = vlan_netdev

        if "bonds" in ns.config:
            bond_dict = self.render_bonds(ns)
            bond_netdev = bond_dict["bond_netdev"]
            bond_link = bond_dict["bond_link"]
            ret_dict["bond_netdev"] = bond_netdev

        for iface in ns.iter_interfaces():
            cfg = CfgParser()

            iface_name = iface["name"]

            vlan_link_name = vlan_link.get(iface_name)
            if vlan_link_name:
                cfg.update_section("Network", "VLAN", vlan_link_name)

            # TODO: revisit this once network state renders macaddress
            # properly for vlan config
            if not iface["mac_address"] and vlan_link.get("macaddress"):
                mac = vlan_link["macaddress"].get(iface_name)
                if mac:
                    iface["mac_address"] = mac

            bond_link_name = bond_link.get(iface_name)
            if bond_link_name:
                cfg.update_section("Network", "Bond", bond_link_name)

            # TODO: revisit this once network state renders macaddress
            # properly for bond config
            if not iface["mac_address"] and bond_link.get("macaddress"):
                mac = bond_link["macaddress"].get(iface_name)
                if mac:
                    iface["mac_address"] = mac

            link = self.generate_match_section(iface, cfg)

            self.generate_link_section(iface, cfg)
            dhcp = self.parse_subnets(iface, cfg)
            self.parse_dns(iface, cfg, ns)

            rid = 0
            for route in ns.iter_routes():
                # Use "c" as a dict key prefix for this route to isolate it
                # from other sources of routes
                self.parse_routes(f"c{rid}", route, cfg)
                rid = rid + 1

            if ns.version == 2:
                name: Optional[str] = iface["name"]
                # network state doesn't give dhcp domain info
                # using ns.config as a workaround here

                # Check to see if this interface matches against an interface
                # from the network state that specified a set-name directive.
                # If there is a device with a set-name directive and it has
                # set-name value that matches the current name, then update the
                # current name to the device's name. That will be the value in
                # the ns.config['ethernets'] dict below.
                for dev_name, dev_cfg in ns.config["ethernets"].items():
                    if "set-name" in dev_cfg:
                        if dev_cfg.get("set-name") == name:
                            name = dev_name
                            break
                if name in ns.config["ethernets"]:
                    device = ns.config["ethernets"][name]

                    # dhcp{version}domain are extra keys only present in
                    # VMware config
                    self.dhcp_domain(device, cfg)
                    for version in ["4", "6"]:
                        if (
                            f"dhcp{version}domain" in device
                            and "use-domains"
                            in device.get(f"dhcp{version}-overrides", {})
                        ):
                            exception = (
                                f"{name} has both dhcp{version}domain"
                                f" and dhcp{version}-overrides.use-domains"
                                f" configured. Use one"
                            )
                            raise RuntimeError(exception)

                        self.parse_dhcp_overrides(cfg, device, dhcp, version)

            ret_dict.update({link: cfg.get_final_conf()})

        return ret_dict

    def render_vlans(self, ns: NetworkState) -> dict:
        vlan_link_info: Dict[str, Any] = {}
        vlan_ndev_configs = {}
        vlan_link_info["macaddress"] = {}

        vlans = ns.config.get("vlans", {})
        for vlan_name, vlan_cfg in vlans.items():
            vlan_id = vlan_cfg.get("id")
            parent = vlan_cfg.get("link")

            if vlan_id is None or parent is None:
                LOG.warning(
                    "Skipping VLAN %s - missing 'id' or 'link'", vlan_name
                )
                continue

            vlan_link_info[parent] = vlan_name

            # -------- .netdev for VLAN --------
            cfg = CfgParser()
            cfg.update_section("NetDev", "Name", vlan_name)
            cfg.update_section("NetDev", "Kind", "vlan")

            val = vlan_cfg.get("mtu")
            if val:
                cfg.update_section("NetDev", "MTUBytes", val)

            val = vlan_cfg.get("macaddress")
            if val:
                val = val.lower()
                cfg.update_section("NetDev", "MACAddress", val)
                vlan_link_info["macaddress"][vlan_name] = val

            cfg.update_section("VLAN", "Id", vlan_id)
            vlan_ndev_configs[vlan_name] = cfg.get_final_conf()

        ret_dict = {
            "vlan_netdev": vlan_ndev_configs,
            "vlan_link": vlan_link_info,
        }
        return ret_dict

    def render_bonds(self, ns: NetworkState) -> dict:
        bond_link_info: Dict[str, Any] = {}
        bond_ndev_configs = {}
        section = "Bond"

        bond_link_info["macaddress"] = {}

        bonds = ns.config.get("bonds", {})
        for bond_name, bond_cfg in bonds.items():
            interfaces = bond_cfg.get("interfaces")
            if not interfaces:
                LOG.warning(
                    "Skipping bond %s - missing 'interfaces'", bond_name
                )
                continue

            bond_link_info.update({iface: bond_name for iface in interfaces})

            # -------- .netdev for Bond --------
            cfg = CfgParser()
            cfg.update_section("NetDev", "Name", bond_name)
            cfg.update_section("NetDev", "Kind", "bond")

            val = bond_cfg.get("mtu")
            if val:
                cfg.update_section("NetDev", "MTUBytes", val)

            val = bond_cfg.get("macaddress")
            if val:
                val = val.lower()
                cfg.update_section("NetDev", "MACAddress", val)
                bond_link_info["macaddress"][bond_name] = val

            # Optional bond parameters
            params = bond_cfg.get("parameters", {})

            if "mode" in params:
                cfg.update_section(section, "Mode", params["mode"])

            if "mii-monitor-interval" in params:
                cfg.update_section(
                    section,
                    "MIIMonitorSec",
                    f"{params['mii-monitor-interval']}ms",
                )

            if "updelay" in params:
                cfg.update_section(
                    section, "UpDelaySec", f"{params['updelay']}ms"
                )

            if "downdelay" in params:
                cfg.update_section(
                    section, "DownDelaySec", f"{params['downdelay']}ms"
                )

            if "arp-interval" in params:
                cfg.update_section(
                    section, "ARPIntervalSec", f"{params['arp-interval']}ms"
                )

            if "arp-ip-target" in params:
                targets = params["arp-ip-target"]
                if isinstance(targets, str):
                    targets = [targets]
                ip_list = " ".join(targets)
                cfg.update_section(section, "ARPIPTargets", ip_list)

            if "arp-validate" in params:
                cfg.update_section(
                    section, "ARPValidate", params["arp-validate"]
                )

            if "arp-all-targets" in params:
                cfg.update_section(
                    section, "ARPAllTargets", params["arp-all-targets"]
                )

            if "primary-reselect" in params:
                cfg.update_section(
                    section,
                    "PrimaryReselectPolicy",
                    params["primary-reselect"],
                )

            if "lacp-rate" in params:
                cfg.update_section(
                    section, "LACPTransmitRate", params["lacp-rate"]
                )

            if "transmit-hash-policy" in params:
                cfg.update_section(
                    section,
                    "TransmitHashPolicy",
                    params["transmit-hash-policy"],
                )

            if "ad-select" in params:
                cfg.update_section(section, "AdSelect", params["ad-select"])

            if "min-links" in params:
                cfg.update_section(
                    section, "MinLinks", str(params["min-links"])
                )

            if "all-slaves-active" in params:
                cfg.update_section(
                    section,
                    "AllSlavesActive",
                    str(params["all-slaves-active"]).lower(),
                )

            bond_ndev_configs[bond_name] = cfg.get_final_conf()

        ret_dict = {
            "bond_netdev": bond_ndev_configs,
            "bond_link": bond_link_info,
        }
        return ret_dict


def available(target=None):
    expected = ["ip", "systemctl"]
    search = ["/usr/sbin", "/bin"]
    for p in expected:
        if not subp.which(p, search=search):
            return False
    return True
