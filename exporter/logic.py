import requests
import re
from abc import ABC

# API reference: https://gitpython.readthedocs.io/en/stable/reference.html
import git

from .helpers import ensure_tmp_dir


class GitHubClient:
    """
    This class can communicate with the GitHub API
    just give it a token and go.

    API documentation: https://docs.github.com/en/free-pro-team@latest/rest/reference
    """
    API = 'https://api.github.com'

    def __init__(self, token, session=None):
        self.token = token
        self.session = session or requests.Session()
        self.session.headers = {'User-Agent': 'exporter'}
        self.session.auth = self._token_auth
        self._login = None

    @property
    def login(self):
        """Return user login name associated with token"""
        if self._login is None:
            self._login = self.user().get('login')
        return self._login

    def _token_auth(self, req):
        req.headers['Authorization'] = 'token ' + self.token
        return req

    def _paginated_json_get(self, url, params=None):
        r = self.session.get(url=url, params=params)
        r.raise_for_status()
        json = r.json()
        if 'next' in r.links and 'url' in r.links['next']:
            json += self._paginated_json_get(r.links['next']['url'], params)
        return json

    def _post(self, url, data=None):
        r = self.session.post(url=url, json=data)
        r.raise_for_status()

    def _delete(self, url):
        r = self.session.delete(url=url)
        r.raise_for_status()

    def user(self):
        """Return all user information"""
        return self._paginated_json_get(f'{self.API}/user')

    def get_all_repos(self):
        return self._paginated_json_get(f'{self.API}/user/repos')

    def delete_repo(self, repo_name, owner):
        self._delete(f'{self.API}/repos/{owner}/{repo_name}')

    def create_repo(self, repo_name, data=None):
        data = data or dict()
        data['name'] = repo_name
        self._post(f'{self.API}/user/repos', data)


class GitLabClient:
    """
    This class can communicate with the GitLab API
    just give it a token and go.

    API documentation: https://gitlab.fit.cvut.cz/help/api/README.md
    """
    API = 'https://gitlab.fit.cvut.cz/api/v4'

    def __init__(self, token, session=None):
        self.token = token
        self.session = session or requests.Session()
        self.session.headers = {'User-Agent': 'exporter'}
        self.session.auth = self._token_auth

    def _token_auth(self, req):
        req.headers['Private-Token'] = self.token
        return req

    def _paginated_json_get(self, url, params=None):
        r = self.session.get(url=url, params=params)
        r.raise_for_status()
        json = r.json()
        if 'next' in r.links and 'url' in r.links['next']:
            json += self._paginated_json_get(r.links['next']['url'], params)
        return json

    def user(self):
        return self._paginated_json_get(f'{self.API}/user')

    def get_all_owned_projects(self):
        return self._paginated_json_get(f'{self.API}/projects', params={'owned': True})

    def search_owned_projects(self, search):
        return self._paginated_json_get(f'{self.API}/projects', params={'owned': True, 'search': search})


class Task(ABC):

    def run(self):
        pass


class TaskFetchGitlabProject(Task):

    def __init__(self, gitlab, project_name, base_dir):
        self.gitlab = gitlab
        self.project_name = project_name
        self.base_dir = base_dir

    def run(self):
        r = self.gitlab.search_owned_projects(self.project_name)
        if len(r) == 0:
            raise ValueError(f'Multiple projects found for {self.project_name}')
        if len(r[0]) == 0:
            raise ValueError(f'No project found for {self.project_name}')
        json = r[0]

        username = json['owner']['username']
        password = self.gitlab.token
        url = json['http_url_to_repo']
        auth_https_url = re.sub(r'(https://)', f'\\1{username}:{password}@', url)
        repo = git.Repo.clone_from(auth_https_url, self.base_dir / self.project_name)
        git.cmd.Git(working_dir=repo.working_dir).execute(['git', 'lfs', 'fetch', '--all'])  # fetching all references
        return repo


class TaskPushToGitHub(Task):

    def __init__(self, github, git_repo, repo_name):
        self.github = github
        self.git_repo = git_repo
        self.repo_name = repo_name

    def run(self):
        self.github.create_repo(self.repo_name)
        owner = self.github.login
        password = self.github.token
        auth_https_url = f'https://{owner}:{password}@github.com/{owner}/{self.repo_name}.git'
        remote = self.git_repo.create_remote(f'github_{self.repo_name}', auth_https_url)
        remote.push()


class Exporter:

    def __init__(self, github, gitlab, logger):
        self.github = github
        self.gitlab = gitlab
        self.logger = logger

    def run(self, projects, force_overwrite=False, tmp_dir=None):
        with ensure_tmp_dir(tmp_dir) as tmp:
            for project_name in projects:
                # T1: fetch all projects (in parallel), report errors after this transaction
                # T2: for each project, create corresponding GitHub repo (in parallel)
                # T3: upload downloaded projects to GitHub (parallel)

                # TODO:
                # a) rollback feature -> remove unused GitHub repos
                # b) delete files from disc
                # c) concurrency issues -> logging, reporting to user (report after each transaction ??)
                # d) check for already existing github repository names

                task = TaskFetchGitlabProject(self.gitlab, project_name, tmp)
                repo = task.run()

                task2 = TaskPushToGitHub(self.github, repo, project_name)
                task2.run()
