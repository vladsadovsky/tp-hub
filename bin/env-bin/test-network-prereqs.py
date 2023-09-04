#!/usr/bin/env python3

import dotenv

import re
import os
import sys
import argparse
import json
import urllib3
import logging
import time
from secrets import token_bytes

from tp_hub.config import HubSettings, current_hub_settings
from tp_hub.internal_types import *

from tp_hub import (
    install_docker,
    docker_is_installed,
    install_docker_compose,
    docker_compose_is_installed,
    create_docker_network,
    create_docker_volume,
    should_run_with_group,
    get_public_ip_address,
    get_gateway_lan_ip_address,
    get_lan_ip_address,
    get_default_interface,
    logger,
    docker_compose_call,
    download_url_text,
    get_public_ip_address,
    get_lan_ip_address,
    DockerComposeStack,
    resolve_public_dns,
    get_project_dir,
    get_project_bin_data_dir,
    resolve_public_dns,
  )

subdomains = [ "ddns", "traefik", "portainer", "hub", "whoami" ]
ports = [ 80, 443, 7080, 7443, 8080, 9000 ]

class App:
    project_dir: str
    project_env_file: str
    hub_test_compose_file: str

    _whoami_get_re = re.compile(r"^(?P<verb>GET|POST)\s+(?P<path>.*[^\s])\s+HTTP/(?P<http_version>\d+\.\d+)\s*$")

    def parse_whoami_response(self, response: str) -> Dict[str, Union[str, List[str]]]:
        """Parse the response from a whoami server"""
        result: Dict[str, Union[str, List[str]]] = {}

        for line in response.splitlines():
            if line.strip() == "":
                continue
            m = self._whoami_get_re.match(line)
            if m is not None:
                result["http-verb"] = m.group("verb")
                result["http-path"] = m.group("path")
                result["http-version"] = m.group("http_version")
            else:
                if ":" not in line:
                    raise HubError(f"Invalid whoami response line (no colon): {line!r}")
                key, value = line.split(":", 1)
                key = key.strip()
                value = value.strip()
                if key in result:
                    if isinstance(result[key], list):
                        result[key].append(value)
                    else:
                        result[key] = [result[key], value]
                else:
                    result[key] = value
        return result

    def test_whoami_port_connection(
            self,
            remote_host: str,
            remote_port: int,
            *,
            expected_hostname: Optional[str]=None,
            stripped_path_prefix: Optional[str]=None,
            connect_timeout: float=5.0,
            read_timeout: float=5.0,
        ):
        """Test whether a whoami server can be reachedat a given host and port"""
        logger.debug(f"test_whoami_port_connection: Testing connection to {remote_host}:{remote_port}")

        if stripped_path_prefix is not None:
            if stripped_path_prefix == "":
                stripped_path_prefix = None
            elif not stripped_path_prefix.startswith("/"):
                stripped_path_prefix = "/" + stripped_path_prefix


        req_timeout = urllib3.Timeout(connect=connect_timeout, read=read_timeout)
        pool_manager = urllib3.PoolManager(timeout=req_timeout)
        try:
            random_path = token_bytes(16).hex()

            response = download_url_text(f"http://{remote_host}:{remote_port}/{random_path}", pool_manager=pool_manager)
            headers = self.parse_whoami_response(response)
        finally:
            pool_manager.clear()
        http_path = headers.get("http-path")
        if http_path is None:
            raise HubError(f"whoami server did not return an HTTP path")
        if stripped_path_prefix is not None:
            http_path = stripped_path_prefix + http_path
        expected_http_path = f"/{random_path}"
        if http_path != expected_http_path:
            raise HubError(f"whoami server returned nonmatching HTTP path (correct is {expected_http_path!r}): {http_path!r}")
        hostname = headers.get("Hostname")
        if hostname is None:
            raise HubError(f"test_whoami_port_connection: whoami server did not return a hostname")
        if expected_hostname is not None and hostname != expected_hostname:
            raise HubError(f"whoami server returned nonmatching hostname (correct is {expected_hostname!r}): {hostname!r}")
        logger.debug(f"test_whoami_port_connection: whoami server at {remote_host}: {remote_port} responded correctly")

    def test_server_traefik_ports(self, public_ip_address: str):
        """Test whether all traefik ports are available to listen on and
        in the case of 7080 and 7443, are properly port-forwarded from the gateway router

        Useful for checking port forwarding rules.

        Actually listens on the traefik listener ports, which must not be in use. Traefik must be shut down
        before performing this test.
        """

        print(f"Testing availability and connectivity of all public and LAN-local hub ports using temporary whoami stub servers", file=sys.stderr)
        random_hostname_suffix = "-" + token_bytes(16).hex()
        expected_hostname = f"port-test{random_hostname_suffix}"

        lan_ip_address = get_lan_ip_address()

        stack_created = False

        try:
            with DockerComposeStack(
                    compose_file=self.hub_test_compose_file,
                    additional_env=dict(HOSTNAME_SUFFIX=random_hostname_suffix),
                    auto_down_on_enter=True,
                    auto_up=True,
                    auto_down=True,
                    up_stderr_exception=True,
                ) as stack:
                stack_created = True
                time.sleep(4.0)   # let services start up
                for port in ports:
                    try:
                        self.test_whoami_port_connection(lan_ip_address, port, expected_hostname=expected_hostname)
                        print(f"Test for hub LAN port {lan_ip_address}:{port} passed!", file=sys.stderr)
                    except Exception as e:
                        raise HubError(f"Test for hub LAN port {lan_ip_address}:{port} failed") from e
                for port in [ 80, 443 ]:
                    try:
                        self.test_whoami_port_connection(public_ip_address, port, expected_hostname=expected_hostname)
                        print(f"Test for Gateway public port {public_ip_address}:{port} forwarding to {lan_ip_address}:{port+7000} passed!", file=sys.stderr)
                    except Exception as e:
                        raise(f"Test for Gateway public port {public_ip_address}:{port} forwarding to {lan_ip_address}:{port+7000} failed") from e
        except Exception as e:
            if not stack_created:
                raise HubError(f"Unable to launch port test stack. Check to ensure Traefik is not running") from e
            raise
        logger.debug("All port tests passed!")

    def test_public_dns_name(self, hostname: str):
        """Test whether a public DNS name resolves to the public IP address"""
        print(f"Testing whether public DNS name {hostname} resolves to public IP address", file=sys.stderr)
        public_ip_address = get_public_ip_address()
        resolved_ip_addresses = resolve_public_dns(hostname)
        if not public_ip_address in resolved_ip_addresses:
            raise HubError(f"Public DNS name {hostname} resolves to {resolved_ip_addresses}, not {public_ip_address}")
        if len(resolved_ip_addresses) > 1:
            raise HubError(f"Public DNS name {hostname} resolves to multiple IP addresses: {resolved_ip_addresses}, not just {public_ip_address}")
        print(f"Public DNS name {hostname} resolves correctly to public IP address {public_ip_address}", file=sys.stderr)
        
    def main(self) -> int:
        parser = argparse.ArgumentParser(description="Test network and port configuration prerequisites for this project")

        parser.add_argument( '--loglevel', type=str.lower, default='warning',
                    choices=['debug', 'info', 'warning', 'error', 'critical'],
                    help='Provide logging level. Default="warning"' )

        args = parser.parse_args()
        logging.basicConfig(level=args.loglevel.upper())

        config = current_hub_settings()
        stable_public_dns_name = config.stable_public_dns_name
        stable_public_ip_addresses = resolve_public_dns(stable_public_dns_name)
        if len(stable_public_ip_addresses) == 0:
            raise HubError(f"Stable public DNS name {stable_public_dns_name} does not resolve to any IP addresses")
        if len(stable_public_ip_addresses) > 1:
            raise HubError(f"Stable public DNS name {stable_public_dns_name} resolves to multiple IP addresses: {stable_public_ip_addresses}")
        stable_public_ip_address = stable_public_ip_addresses[0]

        self.hub_test_compose_file = os.path.join(get_project_bin_data_dir(), "hub_port_test", "docker-compose.yml")

        local_public_ip_addr = get_public_ip_address()
        if local_public_ip_addr != stable_public_ip_address:
            print(f"WARNING: Gateway's Public IP address {local_public_ip_addr} "
                  f"does not match stable public IP address {stable_public_ip_address}", file=sys.stderr)
        gateway_lan_ip_addr = get_gateway_lan_ip_address()
        lan_ip_addr = get_lan_ip_address()
        default_interface = get_default_interface()
        username = os.environ["USER"]

        print(f"Testing network prerequisites for this project", file=sys.stderr)
        traefik_dns_domain = config.parent_dns_domain
        print(f"Traefik DNS domain: {traefik_dns_domain}", file=sys.stderr)

        for subdomain in subdomains:
            self.test_public_dns_name(f"{subdomain}.{traefik_dns_domain}")

        self.test_server_traefik_ports(stable_public_ip_address)

        if should_run_with_group("docker"):
            print("\nWARNING: docker and docker-compose require membership in OS group 'docker', which was newly added for", file=sys.stderr)
            print(f"user \"{username}\", and is not yet effective for the current login session. Please logout", file=sys.stderr)
            print("and log in again, or in the mean time run docker with:\n", file=sys.stderr)
            print(f"      sudo -E -u {username} docker [<arg>...]", file=sys.stderr)

        print(file=sys.stderr)
        fqdomains = [ f"{subdomain}.{traefik_dns_domain}" for subdomain in subdomains]
        print(f"{fqdomains} all resolve to {stable_public_ip_address}", file=sys.stderr)
        print(f"Ports {ports}} are all available for use.", file=sys.stderr)
        print(f"Port forwarding from {stable_public_ip_address}:80 to {lan_ip_addr}:7080 is working.", file=sys.stderr)
        print(f"Port forwarding from {stable_public_ip_address}:443 to {lan_ip_addr}:7443 is working.", file=sys.stderr)
        print(f"DDNS stable public DNS name: {stable_public_dns_name}", file=sys.stderr)
        print(f"DDNS current public IP address: {stable_public_ip_address}", file=sys.stderr)
        print(f"LAN default route interface: {default_interface}", file=sys.stderr)
        print(f"LAN IP address: {lan_ip_addr}", file=sys.stderr)
        print(f"Gateway LAN IP address: {gateway_lan_ip_addr}", file=sys.stderr)
        print(f"Gateway Public IP address: {local_public_ip_addr}", file=sys.stderr)

        if local_public_ip_addr != stable_public_ip_address:
            print(f"\nWARNING: Gateway's public IP address {local_public_ip_addr} "
                  f"does not match current DDNS public IP address {stable_public_ip_address}, but tests passed anyway (VLAN tunnel?).", file=sys.stderr)
            
        print("\nAll network prerequisite tests passed!", file=sys.stderr)

        return 0


if __name__ == "__main__":
    rc = App().main()
    sys.exit(rc)
