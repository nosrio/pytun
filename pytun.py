import argparse
import configparser
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures.thread import ThreadPoolExecutor
from multiprocessing import freeze_support
from os import listdir
from os.path import isabs, dirname, realpath
from os.path import isfile, join
import coloredlogs
from paramiko import BadHostKeyException, PasswordRequiredException, AuthenticationException, SSHException
import psutil

from alerts.email_alert import EmailAlertSender
from alerts.http_post_alert import HTTPPostAlertSender
from alerts.pooled_alerter import DifferentThreadAlert
from configure_logger import LogManager
from observation.connection_check import ConnectionCheck
from observation.http_server import inspection_http_server
from observation.status import Status
from tunnel_infra.TunnelProcess import TunnelProcess
from tunnel_infra.pathtype import PathType
from version import __version__

freeze_support()

INI_FILENAME = 'connector.ini'


def main():
    parser = argparse.ArgumentParser(description='Tunnel')
    parser.add_argument("--config_ini", dest="config_ini", help="Configuration file to use", default=INI_FILENAME,
                        type=PathType(dash_ok=False))
    parser.add_argument("--test_smtp", dest="test_mail", help="Send a test email to validate the smtp config and exits",
                        action='store_true', default=False)
    parser.add_argument("--test_http", dest="test_http", help="Send a test post to validate the http config and exits",
                        action='store_true', default=False)
    parser.add_argument("--test_connections", dest="test_connections",
                        help="Test to connect to the exposed services for each connector", action='store_true',
                        default=False)
    parser.add_argument("--test_tunnels", dest="test_connectors",
                        help="Test to establish each one of the connectors", action='store_true',
                        default=False)
    parser.add_argument("--test_connectors", dest="test_connectors",
                        help="Test to establish each one of the connectors", action='store_true',
                        default=False)
    parser.add_argument("--test_all", dest="test_all", help="Test connections", action="store_true", default=False)
    parser.add_argument('-v', '--version', action='version', version='%(prog)s ' + __version__)
    args = parser.parse_args()
    config = configparser.ConfigParser()
    if not isabs(args.config_ini):
        ini_path = join(dirname(realpath(__file__)), args.config_ini)
    else:
        ini_path = args.config_ini
    pytun_ini_path = join(dirname(realpath(__file__)), 'pytun.ini')
    if os.path.isfile(pytun_ini_path) and not os.path.isfile(join(dirname(realpath(__file__)), INI_FILENAME)):
        os.rename(pytun_ini_path, join(dirname(realpath(__file__)), INI_FILENAME))
    if os.path.isfile(ini_path):
        config.read(ini_path)
        if 'config-connector' in config:
            params = config['config-connector']
        else:
            params = config['pytun']
    else:
        params = {}
    test_something = args.test_mail or args.test_http or args.test_connections or args.test_connectors
    tunnel_manager_id = params.get("tunnel_manager_id", '')
    log_path = params.get("log_path", './logs')
    if not isabs(log_path):
        log_path = join(dirname(realpath(__file__)), log_path)
        # Hack: sometimes when running on windows with pyinstaller and shawl a "\\?\" is added to cwd and it fails
        if log_path.startswith("\\\\?\\"):
            log_path = log_path.replace("\\\\?\\", "")
        if not os.path.isdir(log_path):
            os.mkdir(log_path)
    LogManager.path = log_path
    TunnelProcess.default_log_path = log_path
    logger = LogManager.configure_logger('main_connector.log', params.get("log_level", "INFO"), test_something)
    if tunnel_manager_id is None:
        logger.error("tunnel_manager_id not set in the config file")
        sys.exit(1)
    smtp_sender = get_smtp_alert_sender(logger, tunnel_manager_id, params)

    if args.test_mail:
        test_mail_and_exit(logger, smtp_sender)

    post_sender = get_post_alert_sender(logger, tunnel_manager_id, params)

    if args.test_http:
        test_http_and_exit(logger, post_sender)

    tunnel_path = params.get("tunnel_dirs", "configs")

    if not isabs(args.config_ini):
        tunnel_path = join(dirname(realpath(__file__)), tunnel_path)
        # Hack: sometimes when running on windows with pyinstaller and shawl a "\\?\" is added to cwd and it fails
        if tunnel_path.startswith("\\\\?\\"):
            tunnel_path = tunnel_path.replace("\\\\?\\", "")

    files = [join(tunnel_path, f) for f in listdir(tunnel_path) if isfile(join(tunnel_path, f)) and f[-4:] == '.ini']
    processes = {}

    if args.test_connections:
        test_connections_and_exit(files, logger, processes)

    if args.test_connectors:
        test_tunnels_and_exit(files, logger, processes)

    if args.test_all:
        coloredlogs.install(level='DEBUG', logger=logger)
        http_inspection_thread = None

        if params == {}:
            logger.info('Failed to load the ini file.')
        elif tunnel_path is None:
            logger.info('Tunnel path is invalid.')
        else:
            try:
                address = get_inspection_address(params)
                http_inspection = inspection_http_server(tunnel_path, tunnel_manager_id, LogManager.path, Status(),
                                                         __version__,
                                                         address, logger)
                http_inspection_thread = threading.Thread(target=lambda: http_inspection.serve_forever())
                http_inspection_thread.daemon = True
            except OSError as e:
                logger.exception(
                    f"Couldn't start inspection HTTP server. Address {address[0]}:{address[1]} already in use. "
                    f"Exception: {e}")
        test_everything(files, logger, processes, introspection_thread=http_inspection_thread)
        logger.info("Press Enter to continue...")
        input()
        sys.exit(0)

    senders = [x for x in [smtp_sender, post_sender] if x is not None]

    pool = ThreadPoolExecutor(1)
    main_sender = DifferentThreadAlert(senders, pool)

    status = Status()

    start_tunnels(files, logger, processes, senders, status)

    if len(processes) == 0:
        logger.exception("No config files found")
        sys.exit(1)

    register_signal_handlers(processes, pool)

    http_inspection = inspection_http_server(tunnel_path, tunnel_manager_id, LogManager.path, status, __version__,
                                             get_inspection_address(params), logger)
    http_inspection_thread = threading.Thread(target=lambda: http_inspection.serve_forever())
    http_inspection_thread.daemon = True
    http_inspection_thread.start()

    while True:
        items = list(processes.items())
        to_restart = []
        check_tunnels(files, items, logger, processes, to_restart, pool, main_sender)
        restart_tunnels(files, logger, processes, to_restart, senders, status)
        if not http_inspection_thread.is_alive():
            http_inspection_thread.join()
            http_inspection_thread = threading.Thread(target=lambda: http_inspection.serve_forever())
            http_inspection_thread.daemon = True
            http_inspection_thread.start()
        time.sleep(30)


def get_inspection_address(params):
    only_local = bool(params.getboolean('inspection_localhost_only', True))
    return "127.0.0.1" if only_local else "0.0.0.0", params.getint('inspection_port', 9999)


def test_everything(files, logger, processes, introspection_thread=None):
    logger.info("We will check your installation and configuration")
    service_up = test_service_is_running(logger)
    if not service_up:
        service_up = test_service_is_running(logger, service_name='InvgateConnector')
    if not service_up:
        logger.info("The service is not running! You won't be able to access your services from the cloud")
        if introspection_thread:
            introspection_thread.start()
    failed_connection = test_connections(files, logger, processes)
    if not failed_connection:
        logger.info("All the services are reachable!")
    else:
        logger.info("Not all the services were reachable, please check the output")
    if service_up:
        logger.info(
            "We will partially test the tunnels because the service is up. If you need further testing, please stop the service and repeat the test")
    failed_tunnels = test_tunnels(files, logger, test_reverse_forward=not service_up)
    if not failed_tunnels:
        logger.info("All the connectors seem to work!")
    else:
        logger.info("Not all the connectors are working, check the output!")


def test_service_is_running(logger, service_name='InvGateTunnel'):
    logger.info("Going to check the status of the service")
    if os.name == 'nt':
        try:
            service = psutil.win_service_get(service_name)
            service = service.as_dict()
        except Exception as e:
            return False
        logger.info("%s Service is %s", service_name, service['status'])
        return service['status'] == 'running'
    else:
        logger.info("We are not running on windows")
    return False


def test_tunnels_and_exit(files, logger, processes):
    failed = test_tunnels(files, logger)
    if failed:
        logger.error("Some connectors failed!")
        sys.exit(4)
    else:
        logger.info("All the connectors worked!")
        sys.exit(0)


def test_tunnels(files, logger, test_reverse_forward=True):
    failed = False
    for each in range(len(files)):
        try:
            config_file = files[each]
            logger.info("Going to start connector from file %s", config_file)
            try:
                tunnel_process = TunnelProcess.from_config_file(config_file, [])
            except Exception as e:
                logger.exception(
                    "Failed to create connector from file %s. Configuration file may be incorrect. Error detail %s",
                    config_file, e)
                failed = True
                continue
            tunnel_process.logger = logger
            try:
                client = tunnel_process.ssh_connect(exit_on_failure=False)
                transport = client.get_transport()
            except socket.timeout as e:
                message = """Failed to connect with  %s:%s. We received a connection timeout. Please check that you have internet access, that you can access to %s using telnet. Error %r"""
                logger.exception(message % (tunnel_process.server_host, tunnel_process.server_port,
                                            (tunnel_process.server_host, tunnel_process.server_port), e))
                failed = True
                continue
            if test_reverse_forward:
                try:
                    transport.request_port_forward("", tunnel_process.remote_port_to_forward)
                    transport.close()
                except SSHException as e:
                    message = """Failed to connect with service %s:%s. We received a Port binding rejected error. That means that we could not open our connector completely.
                                            Please check server_host, server_port and port in your config.
                                            Error %r"""
                    logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e))
                    failed = True
                    continue
            client.close()
        except BadHostKeyException as e:
            message = """Failed to connect with service %s:%s. The host key given by the SSH server did not match what 
            we were expecting.
            The hostname was %s, 
            the expected key was %s, 
            the key that we got was %s
            Please check server_key in your config.
            Detailed Error %r"""
            logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e.hostname,
                                        e.expected_key.get_base64(), e.key.get_base64(), e))
            failed = True
        except AuthenticationException as e:
            message = """Failed to connect with service %s:%s. The private key file was rejected. 
                                    Please check keyfile in your config
                                    Error %r"""
            logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e))
            failed = True
        except PasswordRequiredException as e:
            message = """Failed to connect with service %s:%s. The private key file is encrypted. 
                        Please check keyfile and username in your config
                        Error %r"""
            logger.exception(message % (tunnel_process.remote_host, tunnel_process.remote_port, e))
            failed = True

        except Exception as e:
            failed = True
            logger.exception("Failed to establish connector %s with error %r" %
                             (tunnel_process.tunnel_name, e))
    return failed


def test_internet_access(logger):
    host = "8.8.8.8"
    port = 53
    try:
        socket.setdefaulttimeout(3)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        logger.info("It seems that we are able to access internet")
        return True
    except socket.error as ex:
        logger.error("It seems that the server DOES NOT have internet access")
        return False


def test_connections_and_exit(files, logger, processes):
    failed = test_connections(files, logger, processes)
    if failed:
        logger.error("Some connections failed!")
        sys.exit(3)
    else:
        logger.info("All the connections worked!")
        sys.exit(0)


def test_connections(files, logger, processes):
    create_tunnels_from_config([], files, logger, processes)
    failed = False
    test_internet_access(logger)
    for key, tunnel_proc in processes.items():
        with socket.socket() as sock:
            try:
                sock.settimeout(2)
                sock.connect((tunnel_proc.remote_host, tunnel_proc.remote_port))
                logger.info("Connection to %s:%s was successful", tunnel_proc.remote_host, tunnel_proc.remote_port)
            except Exception as e:
                logger.exception(
                    "Failed to connect with service %s:%s. Please check that you have internet access, that there is not a firewall blocking the connection or that remote_host and remote_port in your config are correct. Error %r" %
                    (tunnel_proc.remote_host, tunnel_proc.remote_port, e))
                failed = True
    return failed


def test_mail_and_exit(logger, smtp_sender):
    if smtp_sender is None:
        logger.error("No SMTP config found!")
        sys.exit(2)
    try:
        smtp_sender.send_alert("Testing email", message="Testing email", exception_on_failure=True)
    except Exception as e:
        logger.exception("Failed to send email %r", e)
        sys.exit(1)
    logger.info("Mail test success!")
    sys.exit(0)


def test_http_and_exit(logger, post_sender):
    if post_sender is None:
        logger.error("No http config found!")
        sys.exit(2)
    try:
        post_sender.send_alert("Testing post", message="Testing email", exception_on_failure=True)
    except Exception as e:
        logger.exception("Failed to send post %r", e)
        sys.exit(1)
    logger.info("HTTP post test success!")
    sys.exit(0)


def check_tunnels(files, items, logger, processes, to_restart, pool, pooled_sender):
    for key, proc in items:
        """
        pool.submit(ConnectionCheck(logger, pooled_sender).test_connection, proc.tunnel_name,
                    processes[key].remote_host,
                    processes[key].remote_port)
        """
        if (not proc.is_alive()) and proc.exitcode is not None:
            proc.terminate()
            del processes[key]
            to_restart.append(key)
            logger.info("Connector %s is down", files[key])
            pooled_sender.send_alert(proc.tunnel_name)
        else:
            logger.debug("Connector %s is up", files[key])


def restart_tunnels(files, logger, processes, to_restart, alert_senders, status):
    for each in to_restart:
        logger.info("Going to restart connector from file %s", files[each])
        tunnel_process = TunnelProcess.from_config_file(files[each], alert_senders)
        processes[each] = tunnel_process
        tunnel_process.start()
        status.start_tunnel(files[each])
        logger.info("Connector %s has pid %s", tunnel_process.tunnel_name, tunnel_process.pid)


def register_signal_handlers(processes, pool):
    def exit_gracefully(*args, **kwargs):
        if pool:
            pool.shutdown()
        for each in processes.values():
            each.terminate()
        for each in processes.values():
            each.join()

        sys.exit(0)

    signal.signal(signal.SIGINT, exit_gracefully)
    signal.signal(signal.SIGTERM, exit_gracefully)


def start_tunnels(files, logger, processes, alert_senders, status):
    create_tunnels_from_config(alert_senders, files, logger, processes)
    for key, tunnel_process in processes.items():
        tunnel_process.start()
        status.start_tunnel(files[key])
        logger.info("Connector %s has pid %s", tunnel_process.tunnel_name, tunnel_process.pid)


def create_tunnels_from_config(alert_senders, files, logger, processes):
    for each in range(len(files)):
        config_file = files[each]
        logger.info("Going to start connector from file %s", config_file)
        try:
            tunnel_process = TunnelProcess.from_config_file(config_file, alert_senders)
        except Exception as e:
            logger.exception("Failed to create connector from file %s: %s", config_file, e)
            for pr in processes.values():
                pr.terminate()
            sys.exit(1)
        processes[each] = tunnel_process


def get_post_alert_sender(logger, tunnel_manager_id, params):
    if params.get("http_url"):
        try:
            post_sender = HTTPPostAlertSender(tunnel_manager_id, params['http_url'], params['http_user'],
                                              params['http_password'], logger)
        except KeyError as e:
            logger.exception("Missing smtp param %s" % e)
            sys.exit(-1)
    else:
        post_sender = None
    return post_sender


def get_smtp_alert_sender(logger, tunnel_manager_id, params):
    if params.get("smtp_hostname"):
        try:
            smtp_sender = EmailAlertSender(tunnel_manager_id, params['smtp_hostname'], params.get('smtp_login', None),
                                           params.get('smtp_password', None),
                                           params['smtp_to'], logger,
                                           port=params.getint('smtp_port', 25), from_address=params.get('smtp_from'),
                                           security=params.get("smtp_security"))
        except KeyError as e:
            logger.exception("Missing smtp param %s" % e)
            sys.exit(-1)
    else:
        smtp_sender = None
    return smtp_sender


if __name__ == '__main__':
    main()
