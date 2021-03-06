from .routes import _Routes
from .helpers import *
from .http_request_manager import _HttpRequestManager
from .bearer_auth import BearerAuth
from domino._version import __version__

import logging
import requests
from requests.auth import HTTPBasicAuth
import time
import pprint
import re


class Domino:
    def __init__(self, project, api_key=None, host=None, domino_token_file=None):
        self._configure_logging()
        host = get_host_or_throw_exception(host)
        domino_token_file = get_path_to_domino_token_file(domino_token_file)
        api_key = get_api_key(api_key)

        # Initialise request manager
        self.request_manager = self._initialise_request_manager(api_key, domino_token_file)

        owner_username, project_name = project.split("/")
        self._routes = _Routes(host, owner_username, project_name)

        # Get version
        self._version = self.deployment_version().get("version")
        self._logger.info(f"Domino deployment {host} is running version {self._version}")

        # Check version compatibility
        if not is_version_compatible(self._version):
            error_message = f"Domino version: {self._version} is not compatible with " \
                            f"python-domino version: {__version__}"
            self._logger.error(error_message)
            raise Exception(error_message)

    def _configure_logging(self):
        logging.basicConfig(level=logging.INFO)
        self._logger = logging.getLogger(__name__)

    def _initialise_request_manager(self, api_key, domino_token_file):
        if api_key is None and domino_token_file is None:
            raise Exception("Either api_key or path_to_domino_token_file "
                            f"must be provided via class constructor or "
                            f"environment variable")
        elif domino_token_file is not None:
            self._logger.info("Initializing python-domino with bearer token auth")
            return _HttpRequestManager(BearerAuth(domino_token_file))
        else:
            self._logger.info("Fallback: Initializing python-domino with basic auth")
            return _HttpRequestManager(HTTPBasicAuth('', api_key))

    def commits_list(self):
        url = self._routes.commits_list()
        return self._get(url)

    def runs_list(self):
        url = self._routes.runs_list()
        return self._get(url)

    def runs_start(self, command, isDirect=False, commitId=None, title=None,
                   tier=None, publishApiEndpoint=None):

        url = self._routes.runs_start()

        request = {
            "command": command,
            "isDirect": isDirect,
            "commitId": commitId,
            "title": title,
            "tier": tier,
            "publishApiEndpoint": publishApiEndpoint
        }

        response = self.request_manager.post(url, json=request)
        return response.json()

    def runs_start_blocking(self, command, isDirect=False, commitId=None,
                            title=None, tier=None, publishApiEndpoint=None,
                            poll_freq=5, max_poll_time=6000, retry_count=5):
        """
        Run a tasks that runs in a blocking loop that periodically checks to
        see if the task is done.  If the task errors an exception is raised.

        parameters
        ----------
        command : list of strings
                  list that containst the name of the file to run in index 0
                  and args in subsequent positions.
                  example:
                  >> domino.runs_start(["main.py", "arg1", "arg2"])

        isDirect : boolean (Optional)
                   Whether or not this command should be passed directly to
                   a shell.

        commitId : string (Optional)
                   The commitId to launch from. If not provided, will launch
                   from latest commit.

        title    : string (Optional)
                   A title for the run

        tier     : string (Optional)
                   The hardware tier to use for the run. Will use project
                   default tier if not provided.

        publishApiEndpoint : boolean (Optional)
                            Whether or not to publish an API endpoint from the
                            resulting output.

        poll_freq : int (Optional)
                    Number of seconds in between polling of the Domino server
                    for status of the task that is running.

        max_poll_time : int (Optional)
                        Maximum number of seconds to wait for a task to
                        complete. If this threshold is exceeded, an exception
                        is raised.

        retry_count : int (Optional)
                        Maximum number of retry to do while polling
                        (in-case of transient http errors). If this
                        threshold exceeds, an exception is raised.
        """
        run_response = self.runs_start(command, isDirect, commitId, title,
                                       tier, publishApiEndpoint)
        run_id = run_response['runId']

        poll_start = time.time()
        current_retry_count = 0
        while True:
            try:
                run_info = self.get_run_info(run_id)
                current_retry_count = 0
            except requests.exceptions.RequestException as e:
                current_retry_count += 1
                self._logger.warn(f'Failed to get run info for runId: {run_id} : {e}')
                if current_retry_count > retry_count:
                    raise Exception(f'Cannot get run info, max retry {retry_count} exceeded') from None
                else:
                    self._logger.info(f'Retrying ({current_retry_count}/{retry_count}) getting run info ...')
                    time.sleep(poll_freq)
                    continue

            elapsed_time = time.time() - poll_start

            if elapsed_time >= max_poll_time:
                raise Exception('Run \
                                exceeded maximum time of \
                                {} seconds'.format(max_poll_time))

            if run_info is None:
                raise Exception("Tried to access nonexistent run id {}.".
                                format(run_id))

            output_commit_id = run_info.get('outputCommitId')
            if not output_commit_id:
                time.sleep(poll_freq)
                continue

            # once task has finished running check to see if it was successfull
            else:
                stdout_msg = self.runs_stdout(run_id)

                if run_info['status'] != 'Succeeded':
                    header_msg = ("Remote run {0} \
                                  finished but did not succeed.\n"
                                  .format(run_id))
                    raise Exception(header_msg + stdout_msg)

                logging.info(stdout_msg)
                break

        return run_response

    def run_stop(self, runId, saveChanges=True, commitMessage=None):
        """
        :param runId: string
        :param saveChanges: boolean (Optional) Save or discard run results.
        :param commitMessage: string (Optional)
        """
        url = self._routes.run_stop(runId)
        request = {
            "saveChanges": saveChanges,
            "commitMessage": commitMessage,
            "ignoreRepoState": False
        }
        response = self.request_manager.post(url, json=request)

        if response.status_code == 400:
            raise Warning("Run ID:" + runId + " not found.")
        else:
            return response

    def runs_status(self, runId):
        url = self._routes.runs_status(runId)
        return self._get(url)

    def get_run_log(self, runId, includeSetupLog=True):
        """
        Get the unified log for a run (setup + stdout).

        parameters
        ----------
        runId : string
                the id associated with the run.
        includeSetupLog : bool
                whether or not to include the setup log in the output.
        """

        url = self._routes.runs_stdout(runId)

        logs = list()

        if includeSetupLog:
            logs.append(self._get(url)["setup"])

        logs.append(self._get(url)["stdout"])

        return "\n".join(logs)
        
    def get_run_info(self, run_id):
        for run_info in self.runs_list()['data']:
            if run_info['id'] == run_id:
                return run_info

    def runs_stdout(self, runId):
        """
        Get std out emitted by a particular run.

        parameters
        ----------
        runId : string
                the id associated with the run.
        """
        url = self._routes.runs_stdout(runId)
        # pprint.pformat outputs a string that is ready to be printed
        return pprint.pformat(self._get(url)['stdout'])

    def files_list(self, commitId, path='/'):
        url = self._routes.files_list(commitId, path)
        return self._get(url)

    def files_upload(self, path, file):
        url = self._routes.files_upload(path)
        return self._put_file(url, file)

    def blobs_get(self, key):
        self._validate_blob_key(key)
        url = self._routes.blobs_get(key)
        return self.request_manager.get_raw(url)

    def fork_project(self, target_name):
        url = self._routes.fork_project(self._project_id)
        request = {"name": target_name}
        response = self.request_manager.post(url, json=request)
        return response

    def endpoint_state(self):
        url = self._routes.endpoint_state()
        return self._get(url)

    def endpoint_unpublish(self):
        url = self._routes.endpoint()
        response = self.request_manager.delete(url)
        return response

    def endpoint_publish(self, file, function, commitId):
        url = self._routes.endpoint_publish()

        request = {
            "commitId": commitId,
            "bindingDefinition": {
                "file": file,
                "function": function
            }
        }

        response = self.request_manager.post(url, json=request)
        return response

    def deployment_version(self):
        url = self._routes.deployment_version()
        return self._get(url)

    def project_create(self, project_name, owner_username=None):
        url = self._routes.project_create()
        data = {
            'projectName': project_name,
            'ownerOverrideUsername': owner_username
        }
        response = self.request_manager.post(url, data=data, headers=self._csrf_no_check_header)
        return response

    def collaborators_get(self):
        self.requires_at_least("1.53.0.0")
        url = self._routes.collaborators_get()
        return self._get(url)

    def collaborators_add(self, usernameOrEmail, message=""):
        self.requires_at_least("1.53.0.0")
        url = self._routes.collaborators_add()
        data = {
            'collaboratorUsernameOrEmail': usernameOrEmail,
            'welcomeMessage': message
        }
        response = self.request_manager.post(url, data=data, allow_redirects=False,
                                             headers=self._csrf_no_check_header)
        return response

    # App functions
    def app_publish(self, unpublishRunningApps=True, hardwareTierId=None):
        if unpublishRunningApps is True:
            self.app_unpublish()
        app_id = self._app_id
        if app_id is None:
            # No App Exists creating one
            app_id = self.__app_create(hardware_tier_id=hardwareTierId)
        url = self._routes.app_start(app_id)
        request = {
            'hardwareTierId': hardwareTierId
        }
        response = self.request_manager.post(url, json=request)
        return response

    def app_unpublish(self):
        app_id = self._app_id
        if app_id is None:
            return
        url = self._routes.app_stop(app_id)
        response = self.request_manager.post(url)
        return response

    def __app_create(self, name: str = "", hardware_tier_id: str = None) -> str:
        """
        Private method to create app

        :param name: Optional field to set title of app
        :param hardware_tier_id: Optional field to override hardware tier
        :return: Id of newly created App (Un-Published)
        """
        url = self._routes.app_create()
        request_payload = {
            'modelProductType': 'APP',
            'projectId': self._project_id,
            'name': name,
            'owner': '',
            'created': time.time(),
            'lastUpdated': time.time(),
            'status': '',
            'media': [],
            'openUrl': '',
            'tags': [],
            'stats': {
                'usageCount': 0
            },
            'appExtension': {
                'appType': ''
            },
            'id': '000000000000000000000000',
            'permissionsData': {
                'visibility': 'GRANT_BASED',
                'accessRequestStatuses': {},
                'pendingInvitations': [],
                'discoverable': True,
                'appAccessStatus': 'ALLOWED'
            }
        }
        r = self.request_manager.post(url, json=request_payload)
        response = r.json()
        key = "id"
        if key in response.keys():
            app_id = response[key]
        else:
            raise Exception("Cannot create app")
        return app_id

    # Environment functions
    def environments_list(self):
        self.requires_at_least("2.5.0")
        url = self._routes.environments_list()
        return self._get(url)

    # Model Manager functions
    def models_list(self):
        self.requires_at_least("2.5.0")
        url = self._routes.models_list()
        return self._get(url)

    def model_publish(self, file, function, environment_id, name,
                      description, files_to_exclude=[]):
        self.requires_at_least("2.5.0")

        url = self._routes.model_publish()

        request = {
            "name": name,
            "description": description,
            "projectId": self._project_id,
            "file": file,
            "function": function,
            "excludeFiles": files_to_exclude,
            "environmentId": environment_id
        }

        response = self.request_manager.post(url, json=request)
        return response.json()

    def model_versions_get(self, model_id):
        self.requires_at_least("2.5.0")
        url = self._routes.model_versions_get(model_id)
        return self._get(url)

    def model_version_publish(self, model_id, file, function, environment_id,
                              name, description, files_to_exclude=[]):
        self.requires_at_least("2.5.0")

        url = self._routes.model_version_publish(model_id)

        request = {
            "name": name,
            "description": description,
            "projectId": self._project_id,
            "file": file,
            "function": function,
            "excludeFiles": files_to_exclude,
            "environmentId": environment_id
        }

        response = self.request_manager.post(url, json=request)
        return response.json()

    # Helper methods
    def _get(self, url):
        return self.request_manager.get(url).json()

    def _put_file(self, url, file):
        return self.request_manager.put(url, data=file)

    @staticmethod
    def _validate_blob_key(key):
        regex = re.compile("^\\w{40,40}$")
        if not regex.match(key):
            raise Exception(("Blob key should be 40 alphanumeric characters. "
                             "Perhaps you passed a file path on accident? "
                             "If you have a file path and want to get the "
                             "file, use files_list to get the blob key."))

    def requires_at_least(self, at_least_version):
        if at_least_version > self._version:
            raise Exception("You need at least version {} but your deployment \
                            seems to be running {}".format(
                at_least_version, self._version))

    # Workaround to get project ID which is needed for some model functions
    @property
    def _project_id(self):
        url = self._routes.find_project_by_owner_name_and_project_name_url()
        key = "id"
        response = self._get(url)
        if key in response.keys():
            return response[key]

    # This will fetch app_id of app in current project
    @property
    def _app_id(self):
        url = self._routes.app_list(self._project_id)
        response = self._get(url)
        if len(response) != 0:
            app = response[0]
        else:
            return None
        key = "id"
        if key in app.keys():
            app_id = app[key]
        else:
            app_id = None
        return app_id

    _csrf_no_check_header = {'Csrf-Token': 'nocheck'}
