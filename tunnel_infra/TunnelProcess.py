import configparser
import multiprocessing
import signal
import sys

import paramiko

SSH_PORT = 22
DEFAULT_PORT = 4000

from .Tunnel import Tunnel
from configure_logger import configure_logger
from os.path import isabs, dirname, realpath, join


class TunnelProcess(multiprocessing.Process):

    def __init__(self, server_host, server_port, server_key, user_to_loging, key_file, remote_port_to_forward,
                 remote_host, remote_port, logger, keep_alive_time):
        self.server_host = server_host
        self.server_port = server_port
        self.server_key = server_key
        self.user_to_loging = user_to_loging
        self.key_file = key_file
        self.remote_port_to_forward = remote_port_to_forward
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.logger = logger
        self.keep_alive_time = keep_alive_time
        self.tunnel = None
        super().__init__()

    def exit_gracefully(self, *args):
        self.logger.info("Exit gracefully called for %s", self.pid)
        if self.tunnel:
            self.tunnel.stop()
            self.tunnel = None
        sys.exit(0)

    def run(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)
        client = paramiko.SSHClient()
        if self.server_key:
            client.load_system_host_keys(self.server_key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        self.logger.info("Connecting to ssh host %s:%d ..." % (self.server_host, self.server_port))
        try:
            client.connect(
                self.server_host,
                self.server_port,
                username=self.user_to_loging,
                key_filename=self.key_file,
                timeout=10
            )
        except Exception as e:
            self.logger.info("Failed to connect to %s:%d: %r" % (self.server_host, self.server_port, e))
            sys.exit(1)

        self.logger.info(
            "Now forwarding remote port %d to %s:%d ..."
            % (self.remote_port_to_forward, self.remote_host, self.remote_port)
        )
        try:
            tunnel = Tunnel(self.remote_port_to_forward, self.remote_host, self.remote_port, client.get_transport(),
                            self.logger,
                            keep_alive_time=self.keep_alive_time)
            self.tunnel = tunnel
            tunnel.reverse_forward_tunnel()
            sys.exit(0)
        except KeyboardInterrupt:
            if tunnel.timer:
                tunnel.timer.cancel()
            self.logger.info("Port forwarding stopped.")
            sys.exit(0)
        except Exception as e:
            if tunnel.timer:
                tunnel.timer.cancel()
            self.logger.exception("Port forwarding stopped with error %s", e)
            sys.exit(1)

    @staticmethod
    def from_config_file(ini_file, logger=None):
        config = configparser.ConfigParser()
        config.read(ini_file)
        directory = dirname(realpath(ini_file))
        defaults = config['tunnel']
        if logger is None:
            log_level = defaults.get('log_level')
            log_to_console = defaults.get('log_to_console', False)
            logger = configure_logger(log_level, log_to_console)
        server_host = defaults['server_host']
        server_port = int(defaults.get('server_port', SSH_PORT))
        remote_host = defaults['remote_host']
        remote_port = int(defaults.get('remote_port', SSH_PORT))
        remote_port_to_forward = int(defaults.get('port', DEFAULT_PORT))
        key_file = defaults.get('keyfile')
        if key_file is None:
            raise Exception("Missing keyfile argument")
        if not isabs(key_file):
            key_file = join(directory, key_file)
        user_to_loging = defaults["username"]
        server_key = defaults.get("server_key", None)
        if server_key is not None and not isabs(server_key):
            server_key = join(directory, server_key)
        keep_alive_time = int(defaults.get("keep_alive_time", 30))
        tunnel_process = TunnelProcess(server_host, server_port, server_key, user_to_loging, key_file,
                                       remote_port_to_forward, remote_host, remote_port, logger, keep_alive_time)
        return tunnel_process