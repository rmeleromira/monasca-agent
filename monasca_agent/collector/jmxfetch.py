# (C) Copyright 2015-2016 Hewlett Packard Enterprise Development LP

import glob
import logging
import os
import signal
import subprocess
import tempfile
import time

import yaml

import monasca_agent.common.util

log = logging.getLogger(__name__)

JAVA_LOGGING_LEVEL = {
    logging.CRITICAL: "FATAL",
    logging.DEBUG: "DEBUG",
    logging.ERROR: "ERROR",
    logging.FATAL: "FATAL",
    logging.INFO: "INFO",
    logging.WARN: "WARN",
    logging.WARNING: "WARN",
}

JMX_FETCH_JAR_NAME = "jmxfetch-0.3.0-jar-with-dependencies.jar"
JMX_CHECKS = [
    'activemq',
    'activemq_58',
    'cassandra',
    'jmx',
    'solr',
    'tomcat',
]
JMX_COLLECT_COMMAND = 'collect'
JMX_LIST_COMMANDS = {
    'list_everything': 'List every attributes available that has a type supported by JMXFetch',
    'list_collected_attributes': 'List attributes that will actually be collected by your current instances configuration',
    'list_matching_attributes': 'List attributes that match at least one of your instances configuration',
    'list_not_matching_attributes': "List attributes that don't match any of your instances configuration",
    'list_limited_attributes': "List attributes that do match one of your instances configuration but that are not being collected because it would exceed the number of metrics that can be collected",
    JMX_COLLECT_COMMAND: "Start the collection of metrics based on your current configuration and display them in the console"}

PYTHON_JMX_STATUS_FILE = 'jmx_status_python.yaml'

LINK_TO_DOC = "See http://docs.datadoghq.com/integrations/java/ for more information"


class InvalidJMXConfiguration(Exception):
    pass


class JMXFetch(object):
    pid_file = monasca_agent.common.util.PidFile("jmxfetch")
    pid_file_path = pid_file.get_path()

    @classmethod
    def init(cls, confd_path, agent_config, default_check_frequency,
             command=None, checks_list=None, reporter=None):
        try:
            command = command or JMX_COLLECT_COMMAND
            jmx_checks, invalid_checks, java_bin_path, java_options = JMXFetch.should_run(
                confd_path, checks_list)
            if len(invalid_checks) > 0:
                try:
                    JMXFetch.write_status_file(invalid_checks)
                except Exception:
                    log.exception("Error while writing JMX status file")

            if len(jmx_checks) > 0:
                if JMXFetch.is_running() and command == JMX_COLLECT_COMMAND:
                    log.warning("JMXFetch is already running, restarting it.")
                    JMXFetch.stop()

                JMXFetch.start(confd_path, agent_config, java_bin_path,
                               java_options, default_check_frequency,
                               jmx_checks, command, reporter)
                return True
        except Exception:
            log.exception("Error while initiating JMXFetch")

    @classmethod
    def write_status_file(cls, invalid_checks):
        data = {
            'timestamp': time.time(),
            'invalid_checks': invalid_checks
        }
        stream = file(os.path.join(tempfile.gettempdir(), PYTHON_JMX_STATUS_FILE), 'w')
        yaml.dump(data, stream)
        stream.close()

    @classmethod
    def should_run(cls, confd_path, checks_list):
        '''Return a tuple (jmx_checks, invalid_checks, java_bin_path, java_options).

        jmx_checks: list of yaml files that are jmx checks
        (they have the is_jmx flag enabled or they are in JMX_CHECKS)
        and that have at least one instance configured

        invalid_checks: dictionary whose keys are check names that are JMX checks but
        they have a bad configuration. Values of the dictionary are exceptions generated
        when checking the configuration

        java_bin_path: is the path to the java executable. It was
        previously set in the "instance" part of the yaml file of the
        jmx check. So we need to parse yaml files to get it.
        We assume that this value is always the same for every jmx check
        so we can return the first value returned

        java_options: is string contains options that will be passed to java_bin_path
        We assume that this value is always the same for every jmx check
        so we can return the first value returned
        '''

        jmx_checks = []
        java_bin_path = None
        java_options = None
        invalid_checks = {}

        for conf in glob.glob(os.path.join(confd_path, '*.yaml')):
            filename = os.path.basename(conf)
            check_name = filename.split('.')[0]

            if os.path.exists(conf):
                f = open(conf)
                try:
                    check_config = yaml.safe_load(f.read())
                    assert check_config is not None
                    f.close()
                except Exception:
                    f.close()
                    log.error("Unable to parse yaml config in %s" % conf)
                    continue

                try:
                    is_jmx, check_java_bin_path, check_java_options = JMXFetch.is_jmx_check(
                        check_config, check_name, checks_list)
                    if is_jmx:
                        jmx_checks.append(filename)
                        if java_bin_path is None and check_java_bin_path is not None:
                            java_bin_path = check_java_bin_path
                        if java_options is None and check_java_options is not None:
                            java_options = check_java_options
                except InvalidJMXConfiguration as e:
                    log.error("%s check is not a valid jmx configuration: %s" % (check_name, e))
                    invalid_checks[check_name] = e

        return (jmx_checks, invalid_checks, java_bin_path, java_options)

    @classmethod
    def is_jmx_check(cls, check_config, check_name, checks_list):
        init_config = check_config.get('init_config', {})
        java_bin_path = None
        java_options = None
        is_jmx = False
        if init_config is None:
            init_config = {}

        if checks_list:
            if check_name in checks_list:
                is_jmx = True

        elif init_config.get('is_jmx') or check_name in JMX_CHECKS:
            is_jmx = True

        if is_jmx:
            instances = check_config.get('instances', [])
            if not isinstance(instances, list) or len(instances) == 0:
                raise InvalidJMXConfiguration(
                    'You need to have at least one instance defined in the YAML file for this check')

            for inst in instances:
                if not isinstance(inst, dict):
                    raise InvalidJMXConfiguration(
                        "Each instance should be a dictionary. %s" % LINK_TO_DOC)
                host = inst.get('host', None)
                port = inst.get('port', None)
                conf = inst.get('conf', init_config.get('conf', None))
                if host is None:
                    raise InvalidJMXConfiguration("A host must be specified")
                if port is None or not isinstance(port, int):
                    raise InvalidJMXConfiguration("A numeric port must be specified")

                if conf is None:
                    log.warning(
                        "%s doesn't have a 'conf' section. Only basic JVM metrics will be collected. %s" %
                        (inst, LINK_TO_DOC))
                else:
                    if not isinstance(conf, list) or len(conf) == 0:
                        raise InvalidJMXConfiguration(
                            "'conf' section should be a list of configurations %s" % LINK_TO_DOC)

                    for config in conf:
                        include = config.get('include', None)
                        if include is None:
                            raise InvalidJMXConfiguration(
                                "Each configuration must have an 'include' section. %s" %
                                LINK_TO_DOC)

                        if not isinstance(include, dict):
                            raise InvalidJMXConfiguration(
                                "'include' section must be a dictionary %s" % LINK_TO_DOC)

            if java_bin_path is None:
                if init_config and init_config.get('java_bin_path'):
                    # We get the java bin path from the yaml file for backward compatibility
                    # purposes
                    java_bin_path = init_config.get('java_bin_path')

                else:
                    for instance in instances:
                        if instance and instance.get('java_bin_path'):
                            java_bin_path = instance.get('java_bin_path')

            if java_options is None:
                if init_config and init_config.get('java_options'):
                    java_options = init_config.get('java_options')
                else:
                    for instance in instances:
                        if instance and instance.get('java_options'):
                            java_options = instance.get('java_options')

        return is_jmx, java_bin_path, java_options

    @classmethod
    def is_running(cls):
        try:
            pid = JMXFetch.pid_file.get_pid()
            if pid is None:
                return False
        except Exception:
            return False

        if monasca_agent.common.util.get_os() != 'windows':
            try:
                os.kill(pid, 0)
                # os.kill(pid, 0) will throw an exception if pid is not running
                # and won't do anything otherwise
                # It doesn't work on windows as signal.CTRL_C_EVENT is 0, it would quit the process
                return True
            except Exception as e:
                if "Errno 3" not in str(e):
                    log.debug("Couldn't determine if JMXFetch is running. We suppose it's not. %s"
                              % str(e))
                return False

        # Else we are on windows, we need another way to check if it's running
        try:
            import ctypes  # Available from python2.5

            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x100000

            process = kernel32.OpenProcess(SYNCHRONIZE, 0, pid)
            if process != 0:
                kernel32.CloseHandle(process)
                return True
            else:
                return False

        except Exception as e:
            log.debug("Couldn't determine if JMXFetch is running. We suppose it's not. %s" % str(e))
            return False

    @classmethod
    def stop(cls):
        try:
            pid = JMXFetch.pid_file.get_pid()
            if pid is None:
                log.error("Couldn't get jmxfetch pid.")
                return
        except Exception:
            log.error("Couldn't get jmxfetch pid.")
            return

        try:
            log.info("Killing JMX Fetch")
            os.kill(pid, signal.SIGTERM)
            JMXFetch.pid_file.clean()
            try:
                os.remove(os.path.join(tempfile.gettempdir(), PYTHON_JMX_STATUS_FILE))
            except Exception:
                pass
            log.info("Success")
        except Exception:
            log.exception("Couldn't kill jmxfetch pid %s" % pid)

    @classmethod
    def get_path_to_jmxfetch(cls):
        if monasca_agent.common.util.get_os() != 'windows':
            return os.path.realpath(
                os.path.join(os.path.abspath(__file__), "..", "../collector/checks", "libs",
                             JMX_FETCH_JAR_NAME))

        return os.path.realpath(
            os.path.join(os.path.abspath(__file__), "..", "../../", "jmxfetch", JMX_FETCH_JAR_NAME))

    @classmethod
    def start(cls, confd_path, agent_config, path_to_java, java_run_opts,
              default_check_frequency, jmx_checks, command, reporter=None):
        statsd_port = agent_config.get('monasca_statsd_port', "8125")

        if reporter is None:
            reporter = "statsd:%s" % str(statsd_port)

        log.info("Starting jmxfetch:")
        jmx_connector_pid = None
        try:
            path_to_java = path_to_java or "java"
            java_run_opts = java_run_opts or ""
            path_to_jmxfetch = JMXFetch.get_path_to_jmxfetch()
            path_to_status_file = os.path.join(tempfile.gettempdir(), "jmx_status.yaml")

            subprocess_args = [
                path_to_java,  # Path to the java bin
                '-jar',
                r"%s" % path_to_jmxfetch,  # Path to the jmxfetch jar
                # Period of the main loop of jmxfetch in ms
                '--check_period', str(default_check_frequency * 1000),
                # Path of the conf.d directory that will be read by jmxfetch,
                '--conf_directory', r"%s" % confd_path,
                # Log Level: Mapping from Python log level to log4j log levels
                '--log_level', JAVA_LOGGING_LEVEL.get(agent_config.get("log_level"), "INFO"),
                # Path of the log file
                '--log_location', r"%s" % agent_config.get('jmxfetch_log_file'),
                '--reporter', reporter,  # Reporter to use
                # Path to the status file to write
                '--status_location', r"%s" % path_to_status_file,
                command,  # Name of the command
            ]

            subprocess_args.insert(3, '--check')
            for check in jmx_checks:
                subprocess_args.insert(4, check)

            if java_run_opts:
                for opt in java_run_opts.split():
                    subprocess_args.insert(1, opt)

            log.info("Running %s" % " ".join(subprocess_args))
            if reporter != "console":
                cls.subprocess = subprocess.Popen(subprocess_args, close_fds=True)
                jmx_connector_pid = cls.subprocess.pid
                log.debug("JMX Fetch pid: %s" % jmx_connector_pid)

            else:
                subprocess.call(subprocess_args)

        except OSError:
            jmx_connector_pid = None
            log.exception("Couldn't launch JMXTerm. Is java in your PATH?")
        except Exception:
            jmx_connector_pid = None
            log.exception("Couldn't launch JMXFetch")

        # Write pid to pid file
        if jmx_connector_pid is not None:
            try:
                fp = open(JMXFetch.pid_file_path, 'w+')
                fp.write(str(jmx_connector_pid))
                fp.close()
                os.chmod(JMXFetch.pid_file_path, 0o644)
            except Exception:
                log.exception("Unable to write jmxfetch pidfile: %s" % JMXFetch.pid_file_path)
