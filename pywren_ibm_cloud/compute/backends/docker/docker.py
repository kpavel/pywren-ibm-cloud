import os
import json
import sys
import time
import zipfile
import docker
import logging
import paramiko
import requests
import importlib
import subprocess
import multiprocessing

from . import config as docker_config
from pywren_ibm_cloud.utils import version_str, is_unix_system
from pywren_ibm_cloud.version import __version__
from pywren_ibm_cloud.config import TEMP, DOCKER_BASE_FOLDER, DOCKER_FOLDER
from pywren_ibm_cloud.compute.utils import create_function_handler_zip

logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
logging.getLogger('paramiko.transport').setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)


class RemoteClientStartStopContextManager:
    def __init__(self, remote_client=None):
        self.remote_client = remote_client

    def __enter__(self):
        if self.remote_client:
            self.remote_client.create_instance_action('start')
            time.sleep(5)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.remote_client:
            self.remote_client.create_instance_action('stop')


class DockerBackend:
    """
    A wrap-up around Docker APIs.
    """

    def __init__(self, docker_config):
        self.log_active = logger.getEffectiveLevel() != logging.WARNING
        self.config = docker_config
        self.name = 'docker'
        self.host = docker_config['host']
        self.queue = multiprocessing.Queue()
        self.docker_client = None
        self.ssh_credentials = None

        self._is_localhost = self.host in ['127.0.0.1', 'localhost']

        if self._is_localhost:
            try:
                self.docker_client = docker.from_env()
            except Exception:
                pass
        else:
            ssh_key_filename = self.config.get('ssh_key_filename', None)
            self.ssh_credentials = {'username': self.config['ssh_user'],
                                    'password': self.config['ssh_password'],
                                    'key_filename': ssh_key_filename}

        remote_client = None
        if 'remote_client' in self.config:
            remote_client_backend = self.config['remote_client']
            client_location = 'pywren_ibm_cloud.libs.docker.clients.{}'.format(remote_client_backend)
            client = importlib.import_module(client_location)
            RemoteInstanceDockerClient = getattr(client, 'RemoteInstanceDockerClient')
            remote_client = RemoteInstanceDockerClient(self.config[remote_client_backend],
                                                       user_agent=self.config['user_agent'])
            log_msg = 'Using RemoteClient: {}'.format(remote_client_backend)
            logger.info(log_msg)
        self.remote_client_start_stop = RemoteClientStartStopContextManager(remote_client)

        log_msg = 'PyWren v{} init for Docker - Host: {}'.format(__version__, self.host)
        logger.info(log_msg)
        if not self.log_active:
            print(log_msg)

    def _format_runtime_name(self, docker_image_name):
        name = docker_image_name.replace('/', '_').replace(':', '_')
        return 'pywren_{}'.format(name)

    def _unformat_runtime_name(self, runtime_name):
        image_name = runtime_name.replace('pywren_', '')
        image_name = image_name.replace('_', '/', 1)
        image_name = image_name.replace('_', ':', -1)
        return image_name, None

    def _get_default_runtime_image_name(self):
        python_version = version_str(sys.version_info)
        return docker_config.RUNTIME_DEFAULT[python_version]

    def _delete_function_handler_zip(self):
        os.remove(docker_config.FH_ZIP_LOCATION)

    def _ssh_run_remote_command(self, cmd):
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(self.host, **self.ssh_credentials)
        stdin, stdout, stderr = ssh_client.exec_command(cmd)

        out = stdout.read().decode().strip()
        error = stderr.read().decode().strip()
        if self.log_active:
            logger.info(out)
        if error:
            raise Exception('There was an error pulling the runtime: {}'.format(error))
        ssh_client.close()

        return out

    def _ssh_upload_file(self, src, dst):
        ssh_client = paramiko.SSHClient()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(self.host, **self.ssh_credentials)
        ftp_client = ssh_client.open_sftp()
        ftp_client.put(src, dst)
        ftp_client.close()
        ssh_client.close()

    def _init_runtime(self, docker_image_name):
        name = self._format_runtime_name(docker_image_name)

        if self._is_localhost:
            if is_unix_system():
                uid_cmd = "id -u $USER"
                uid = subprocess.check_output(uid_cmd, shell=True).decode().strip()

            if self.docker_client:
                running_containers = self.docker_client.containers.list(filters={'name': 'pywren'})
                running_runtimes = [c.name for c in running_containers]

                if name not in running_runtimes:
                    self.docker_client.containers.run(docker_image_name, entrypoint='python',
                                                      command='/tmp/{}/__main__.py'.format(DOCKER_BASE_FOLDER),
                                                      volumes=['{}:/tmp'.format(TEMP)],
                                                      detach=True, auto_remove=True,
                                                      user=uid, name=name,
                                                      ports={'8080/tcp': docker_config.PYWREN_SERVER_PORT})
                    time.sleep(5)
            else:
                running_runtimes_cmd = "docker ps --format '{{.Names}}' -f name=pywren"
                running_runtimes = subprocess.run(running_runtimes_cmd, shell=True,
                                                  stdout=subprocess.PIPE).stdout.decode()
                if name not in running_runtimes:
                    if is_unix_system():
                        cmd = ('docker run -d --name {} --user {} -v {}:/tmp -p 8080:{}'
                               ' --entrypoint "python" {} /tmp/{}/__main__.py'
                               .format(name, uid, TEMP, docker_config.PYWREN_SERVER_PORT,
                                       docker_image_name, DOCKER_BASE_FOLDER))
                    else:
                        cmd = ('docker run -d --name {}  -v {}:/tmp -p 8080:{}'
                               ' --entrypoint "python" {} /tmp/{}/__main__.py'
                               .format(name, TEMP, docker_config.PYWREN_SERVER_PORT,
                                       docker_image_name, DOCKER_BASE_FOLDER))

                    if not self.log_active:
                        cmd = cmd + " >{} 2>&1".format(os.devnull)
                    res = os.system(cmd)
                    if res != 0:
                        raise Exception('There was an error starting the runtime')
                    time.sleep(5)

        else:
            running_runtimes_cmd = "docker ps --format '{{.Names}}' -f name=pywren"
            running_runtimes = self._ssh_run_remote_command(running_runtimes_cmd)
            used_runtimes_cmd = "docker ps -a --format '{{.Names}}' -f name=pywren"
            used_runtimes = self._ssh_run_remote_command(used_runtimes_cmd)

            if name not in running_runtimes and name in used_runtimes:
                cmd = 'docker rm -f {}'.format(name)
                self._ssh_run_remote_command(cmd)

            cmd = ('docker run -d --name {} --user $(id -u):$(id -g) -v /tmp:/tmp -p 8080:{}'
                   ' --entrypoint "python" {} /tmp/{}/__main__.py'
                   .format(name, docker_config.PYWREN_SERVER_PORT,
                           docker_image_name, DOCKER_BASE_FOLDER))
            if name not in running_runtimes:
                self._ssh_run_remote_command(cmd)
                time.sleep(5)

    def _generate_runtime_meta(self, docker_image_name):
        """
        Extracts installed Python modules from the local machine
        """
        self._init_runtime(docker_image_name)

        r = requests.get('http://{}:{}/preinstalls'.format(self.host, docker_config.PYWREN_SERVER_PORT))
        runtime_meta = r.json()

        if not runtime_meta or 'preinstalls' not in runtime_meta:
            raise Exception(runtime_meta)

        return runtime_meta

    def invoke(self, docker_image_name, memory, payload):
        """
        Invoke the function with the payload. runtime_name and memory
        are not used since it runs in the local machine.
        """
        with self.remote_client_start_stop:

            self._init_runtime(docker_image_name)

            r = requests.post("http://{}:{}/".format(self.host, docker_config.PYWREN_SERVER_PORT), data=json.dumps(payload))
            response = r.json()

        return response['activationId']

    def create_runtime(self, docker_image_name, memory, timeout):
        """
        Pulls the docker image from the docker hub and copies
        the necessary files to the host.
        """
        if docker_image_name == 'default':
            docker_image_name = self._get_default_runtime_image_name()

        create_function_handler_zip(docker_config.FH_ZIP_LOCATION, '__main__.py', __file__)

        if self._is_localhost:
            os.makedirs(DOCKER_FOLDER, exist_ok=True)
            archive = zipfile.ZipFile(docker_config.FH_ZIP_LOCATION)
            for file in archive.namelist():
                archive.extract(file, DOCKER_FOLDER)
            archive.close()

            if self.docker_client:
                self.docker_client.images.pull(docker_image_name)
            else:
                cmd = 'docker pull {}'.format(docker_image_name)
                if not self.log_active:
                    cmd = cmd + " >{} 2>&1".format(os.devnull)
                res = os.system(cmd)
                if res != 0:
                    raise Exception('There was an error pulling the runtime')
            runtime_meta = self._generate_runtime_meta(docker_image_name)
        else:
            with self.remote_client_start_stop:
                self._ssh_upload_file(docker_config.FH_ZIP_LOCATION, '/tmp/pywren_docker.zip')
                cmd = 'rm -R -f /tmp/{} '.format(DOCKER_BASE_FOLDER)
                cmd += '&& mkdir -p /tmp/{} '.format(DOCKER_BASE_FOLDER)
                cmd += '&& unzip /tmp/pywren_docker.zip -d /tmp/{} '.format(DOCKER_BASE_FOLDER)
                cmd += '&& rm /tmp/pywren_docker.zip'
                self._ssh_run_remote_command(cmd)
                cmd = 'docker pull {}'.format(docker_image_name)
                self._ssh_run_remote_command(cmd)
                runtime_meta = self._generate_runtime_meta(docker_image_name)

        self._delete_function_handler_zip()
        return runtime_meta

    def build_runtime(self, docker_image_name, dockerfile):
        """
        Builds a new runtime from a Dockerfile
        """
        raise Exception('You must use an IBM CF/knative built runtime')

    def delete_runtime(self, docker_image_name, memory):
        """
        Deletes a runtime
        """
        if docker_image_name == 'default':
            docker_image_name = self._get_default_runtime_image_name()

        logger.debug('Deleting {} runtime'.format(docker_image_name))
        name = self._format_runtime_name(docker_image_name)
        if self._is_localhost:
            if self.docker_client:
                self.docker_client.containers.stop(name, force=True)
            else:
                cmd = 'docker rm -f {}'.format(name)
                if not self.log_active:
                    cmd = cmd + " >{} 2>&1".format(os.devnull)
                os.system(cmd)
        else:
            with self.remote_client_start_stop:
                cmd = 'docker rm -f {}'.format(name)
                self._ssh_run_remote_command(cmd)

    def delete_all_runtimes(self):
        """
        Delete all created runtimes
        """
        if self._is_localhost:
            if self.docker_client:
                running_containers = self.docker_client.containers.list(filters={'name': 'pywren'})
                for runtime in running_containers:
                    logger.debug('Deleting {} runtime'.format(runtime.name))
                    runtime.stop()
            else:
                list_runtimes_cmd = "docker ps -a -f name=pywren | awk '{print $NF}' | tail -n +2"
                running_containers = subprocess.check_output(list_runtimes_cmd, shell=True).decode().strip()
                for name in running_containers.splitlines():
                    cmd = 'docker rm -f {}'.format(name)
                    if not self.log_active:
                        cmd = cmd + " >{} 2>&1".format(os.devnull)
                    os.system(cmd)
        else:
            with self.remote_client_start_stop:
                list_runtimes_cmd = "docker ps -a -f name=pywren | awk '{print $NF}' | tail -n +2"
                running_containers = self._ssh_run_remote_command(list_runtimes_cmd)
                for name in running_containers.splitlines():
                    cmd = 'docker rm -f {}'.format(name)
                    self._ssh_run_remote_command(cmd)

    def list_runtimes(self, docker_image_name='all'):
        """
        List all the runtimes deployed in the local machine
        return: list of tuples (docker_image_name, memory)
        """
        if docker_image_name == 'default':
            docker_image_name = self._get_default_runtime_image_name()

        runtimes = []

        if self._is_localhost:
            if self.docker_client:
                running_containers = self.docker_client.containers.list(filters={'name': 'pywren'})
                running_runtimes = [c.name for c in running_containers]
            else:
                list_runtimes_cmd = "docker ps -a -f name=pywren | awk '{print $NF}' | tail -n +2"
                running_containers = subprocess.check_output(list_runtimes_cmd, shell=True).decode().strip()
                running_runtimes = running_containers.splitlines()
        else:
            with self.remote_client_start_stop:
                list_runtimes_cmd = "docker ps -a -f name=pywren | awk '{print $NF}' | tail -n +2"
                running_containers = self._ssh_run_remote_command(list_runtimes_cmd)
                running_runtimes = running_containers.splitlines()

        for runtime in running_runtimes:
            name = self._format_runtime_name(docker_image_name)
            if name == runtime or docker_image_name == 'all':
                tag = self._unformat_runtime_name(runtime)
                runtimes.append((tag, None))

        return runtimes

    def get_runtime_key(self, docker_image_name, memory):
        """
        Method that creates and returns the runtime key.
        Runtime keys are used to uniquely identify runtimes within the storage,
        in order to know what runtimes are installed and what not.
        """
        runtime_name = self._format_runtime_name(docker_image_name)
        runtime_key = os.path.join(self.name, self.host, runtime_name)

        return runtime_key
