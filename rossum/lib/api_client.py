import json
import sys
from contextlib import AbstractContextManager
from pathlib import PurePath
from platform import platform
from typing import Any, BinaryIO, Callable, Dict, IO, Iterable, List, Optional, Tuple, Union

import click
import polling2
import requests
import urllib3
from requests import Response
from tenacity import (
    retry,
    stop_after_attempt,
    stop_after_delay,
    wait_fixed,
    retry_if_exception_type,
)

from rossum import __version__, CTX_PROFILE, CTX_DEFAULT_PROFILE
from rossum.configure import get_credential
from . import (
    ORGANIZATIONS,
    APIObject,
    WORKSPACES,
    QUEUES,
    SCHEMAS,
    CONNECTORS,
    HOOKS,
    USERS,
    GROUPS,
    ANNOTATIONS,
)


class RossumException(click.ClickException):
    pass


RequestsFiles = Dict[str, Tuple[Optional[str], Union[IO[bytes], BinaryIO, str]]]

HEADERS = {"User-Agent": f"rossum/{__version__} ({platform()})"}


class APIClient(AbstractContextManager):
    def __init__(
        self,
        context: Optional[dict],
        url: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        use_api_version: bool = True,
        auth_using_token: bool = True,
        max_token_lifetime: Optional[int] = None,
        retry_logic_rules: Optional[Dict] = None,
    ):
        """The APIClient for communication with Rossum API.

        :param context Used for switching profile. Send None for usual requests done for your default profile.
        :param url Rossum API URL. Leave None for communication with the currently used publicly available Rossum API.
        :param user Your username.
        :param password Your password.
        :param use_api_version Leave True if you want to use the latest API version. If set to false,
        specify the Rossum API URL including its version in the url parameter explicitly.
        :param auth_using_token To avoid using login request for each call, leave set to True.
        :param max_token_lifetime Set custom max token lifetime in seconds. Default is the maximum lifetime: 583200s
        or until the Rossum client logs out. Logging out is made when CLI is exited or if RossumClient() is used
        with a with statement.
        :param retry_logic_rules Pass logic rules for built-in retry mechanism that is called when it is currently
        not possible to communicate with Rossum API. The default shape of the dictionary is:
        {"attempts": 3, "wait_s": 5}. "attempts" key states the number of retry attempts. "wait_s" is time in seconds
        the APIClient will wait before retrying again.
        """
        self._url = url
        self._user = user
        self._password = password
        self._use_api_version = use_api_version
        self._auth_using_token = auth_using_token
        self._max_token_lifetime = max_token_lifetime
        self._profile = (context or {}).get(CTX_PROFILE, CTX_DEFAULT_PROFILE)

        self.token: Optional[str] = None
        self.timeout: Optional[float] = None

        self._retry_logic_rules = self.get_retry_logic(retry_logic_rules)

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.logout()

    @classmethod
    def csv(
        cls, context: Optional[dict], url: str = None, user: str = None, password: str = None
    ) -> "APIClient":
        return cls(context, url, user, password, False, False)

    @property
    def user(self) -> str:
        if self._user is None:
            self._user = get_credential("username", self._profile)
        return self._user

    @property
    def password(self) -> str:
        if self._password is None:
            self._password = get_credential("password", self._profile)
        return self._password

    @property
    def url(self) -> str:
        if self._url is None:
            _url = get_credential("url", self._profile).rstrip("/")
            self._url = f'{_url}{"/v1" if self._use_api_version else ""}'
        return self._url

    @staticmethod
    def get_retry_logic(retry_logic_rules: Optional[Dict]):
        retry_logic_rules = retry_logic_rules or {}
        attempts_no = retry_logic_rules.get("attempts", 3)
        wait_s = retry_logic_rules.get("wait_s", 5)
        retry_logic = {
            "reraise": True,
            "stop": (stop_after_attempt(attempts_no) | stop_after_delay(55)),
            "wait": wait_fixed(wait_s),
            "retry": (
                retry_if_exception_type(requests.exceptions.ProxyError)
                | retry_if_exception_type(requests.exceptions.ConnectionError)
                | retry_if_exception_type(urllib3.exceptions.NewConnectionError)
                | retry_if_exception_type(urllib3.exceptions.ConnectTimeoutError)
            ),
        }
        return retry_logic

    def _login_to_api(self, login_data: Dict) -> Response:
        return requests.post(
            f"{self.url}/auth/login", json=login_data, timeout=self.timeout, headers=HEADERS
        )

    def get_token(self) -> str:
        # self.post cannot be used as it is dependent on self.get_token().
        login_data: Dict[str, Union[str, int]] = {"username": self.user, "password": self.password}
        if self._max_token_lifetime:
            login_data["max_token_lifetime_s"] = self._max_token_lifetime
        retry_request = retry(**self._retry_logic_rules)(self._login_to_api)
        response = retry_request(login_data)
        if response.status_code == 401:
            raise RossumException(f"Login failed with the provided credentials.")
        elif not response.ok:
            raise RossumException(f"Invalid response [{response.url}]: {response.text}")

        return response.json()["key"]

    def post(
        self,
        path: Union[str, APIObject],
        data: dict = None,
        expected_status_code: int = 201,
        files: Optional[RequestsFiles] = None,
    ) -> Response:
        return self._request_url(
            "post",
            f"{self.url}/{path}",
            json=data,
            expected_status_code=expected_status_code,
            files=files,
        )

    def patch(self, path: Union[str, APIObject], data: dict) -> Response:
        return self._request_url("patch", f"{self.url}/{path}", json=data)

    def get(self, path: Union[str, APIObject], query: dict = None) -> Response:
        return self._request_url("get", f"{self.url}/{path}", query)

    def get_url(self, url: str, query: dict = None) -> Response:
        return self._request_url("get", url, query)

    def delete_url(self, url: str) -> Response:
        return self._request_url("delete", url, expected_status_code=204)

    def _do_request(self, method: str, url: str, query: dict = None, **kwargs) -> Response:
        auth = self._authentication
        headers = {**HEADERS, **auth.pop("headers", {}), **kwargs.pop("headers", {})}
        response = requests.request(
            method,
            url,
            params=_encode_booleans(query),
            headers=headers,
            timeout=self.timeout,
            **auth,
            **kwargs,
        )
        return response

    def _request_url(
        self, method: str, url: str, query: dict = None, expected_status_code: int = 200, **kwargs
    ) -> Response:
        retry_request = retry(**self._retry_logic_rules)(self._do_request)
        response = retry_request(method, url, query, **kwargs)
        if response.status_code != expected_status_code:
            raise RossumException(f"Invalid response [{response.url}]: {response.text}")
        return response

    def delete(self, to_delete: Dict[str, str], verbose: int = 0, item: str = "annotation") -> None:
        for id_, url in to_delete.items():
            try:
                self.delete_url(url)
            except RossumException as exc:
                click.echo(f'Deleting {item} {id_} caused "{exc}".')
            except Exception as exc:
                click.echo(f'Deleting {item} {id_} caused an unexpected exception: "{exc}".')
                raise RossumException(str(exc))
            else:
                if verbose > 1:
                    click.echo(f"Deleted {item} {id_}.")

    def get_paginated(
        self,
        path: Union[str, APIObject],
        query: Optional[Dict[str, Any]] = None,
        *,
        key: str = "results",
    ) -> Tuple[List[Dict[str, Any]], int]:
        response = self.get(path, query)
        response_dict = response.json()

        res = response_dict[key]
        next_page = response_dict["pagination"]["next"]

        while next_page:
            response = self.get_url(next_page)
            response_dict = response.json()

            res.extend(response_dict[key])
            next_page = response_dict["pagination"]["next"]

        return res, response_dict["pagination"]["total"]

    def _sideload(
        self, objects: List[dict], sideloads: Optional[Iterable[APIObject]] = None
    ) -> List[dict]:
        for sideload in sideloads or []:
            sideloaded, _ = self.get_paginated(sideload)
            sideloaded_dicts = {
                sideloaded_dict["url"]: sideloaded_dict for sideloaded_dict in sideloaded
            }

            def inject_sideloaded(obj: dict) -> dict:
                try:
                    url = obj[sideload.singular]
                except KeyError:
                    obj[sideload.plural] = [
                        sideloaded_dicts[url]
                        for url in obj[sideload.plural]
                        if url in sideloaded_dicts
                    ]
                else:
                    obj[sideload.singular] = sideloaded_dicts.get(url, {})
                return obj

            objects = [inject_sideloaded(o) for o in objects]
        return objects

    @property
    def _authentication(self) -> dict:
        if self._auth_using_token:
            if self.token is None:
                self.token = self.get_token()
            return {"headers": {"Authorization": "Token " + self.token}}
        else:
            return {"auth": (self.user, self.password)}

    def logout(self) -> None:
        if self._auth_using_token:
            self.post("auth/logout", {}, expected_status_code=200)


class RossumClient(APIClient):
    def get_organization(self, organization_id: Optional[int] = None) -> dict:
        if organization_id is None:
            user_details = self.get_user()
            try:
                organization_url = user_details[ORGANIZATIONS.singular]
            except KeyError:
                organization_url = get_json(self.get_url(user_details["url"]))[
                    ORGANIZATIONS.singular
                ]
            res = self.get_url(organization_url)
        else:
            res = self.get(f"{ORGANIZATIONS}/{organization_id}")
        return get_json(res)

    def get_workspaces(
        self, sideloads: Optional[Iterable[APIObject]] = None, *, organization: Optional[int] = None
    ) -> List[dict]:
        query = {}
        if organization:
            query[ORGANIZATIONS.singular] = organization
        workspaces_list, _ = self.get_paginated(WORKSPACES, query=query)
        self._sideload(workspaces_list, sideloads)
        return workspaces_list

    def get_workspace(
        self, id_: Optional[int] = None, sideloads: Optional[Iterable[APIObject]] = None
    ) -> dict:
        if id_ is None:
            try:
                [workspace] = self.get_workspaces()
            except ValueError as e:
                raise RossumException("Workspace ID must be specified.") from e
        else:
            workspace = get_json(self.get(f"{WORKSPACES}/{id_}"))

        self._sideload([workspace], sideloads)
        return workspace

    def get_queues(
        self,
        sideloads: Optional[Iterable[APIObject]] = None,
        *,
        workspace: Optional[int] = None,
        users: Optional[Iterable[int]] = None,
        hooks: Optional[Iterable[int]] = None,
    ) -> List[dict]:
        query: Dict[str, Any] = {}
        if workspace:
            query[WORKSPACES.singular] = workspace
        if users:
            query[USERS.plural] = users
        if hooks:
            query[HOOKS.plural] = hooks
        queues_list, _ = self.get_paginated(QUEUES, query=query)
        self._sideload(queues_list, sideloads)
        return queues_list

    def get_queue(
        self, id_: Optional[int] = None, sideloads: Optional[Iterable[APIObject]] = None
    ) -> dict:
        if id_ is None:
            try:
                [queue] = self.get_queues()
            except ValueError as e:
                raise RossumException("Queue ID must be specified.") from e
        else:
            queue = get_json(self.get(f"{QUEUES}/{id_}"))

        self._sideload([queue], sideloads)
        return queue

    def get_users(
        self,
        sideloads: Optional[Iterable[APIObject]] = None,
        *,
        username: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> List[dict]:
        query: Dict[str, Union[str, bool]] = {}
        if username:
            query["username"] = username
        if is_active is not None:
            query["is_active"] = is_active
        users_list, _ = self.get_paginated(USERS, query=query)
        self._sideload(users_list, sideloads)
        return users_list

    def get_user(self, id_: Optional[int] = None) -> dict:
        if id_ is None:
            user = get_json(self.get("auth/user"))
        else:
            user = get_json(self.get(f"{USERS}/{id_}"))
        return user

    def get_groups(self, *, group_name: Optional[str]) -> List[dict]:
        if group_name is None:
            return []
        groups_list, _ = self.get_paginated(GROUPS, query={"name": group_name})
        return groups_list

    def get_connectors(self, sideloads: Optional[Iterable[APIObject]] = None) -> List[dict]:
        connectors_list, _ = self.get_paginated(CONNECTORS)
        self._sideload(connectors_list, sideloads)
        return connectors_list

    def get_hooks(
        self, sideloads: Optional[Iterable[APIObject]] = None, query: Dict = None
    ) -> List[dict]:
        hooks_list, _ = self.get_paginated(HOOKS, query)
        self._sideload(hooks_list, sideloads)
        return hooks_list

    def get_annotation(self, id_: Optional[int] = None) -> dict:
        if id_ is None:
            raise RossumException("Annotation ID wasn't specified.")
        return get_json(self.get(f"{ANNOTATIONS}/{id_}"))

    def poll_annotation(
        self, annotation: int, check_success: Callable, max_retries=120, sleep_secs=5
    ) -> dict:
        return polling2.poll(
            lambda: self._get_annotation_polling(annotation),
            check_success=check_success,
            step=sleep_secs,
            timeout=int(round(max_retries * sleep_secs)),
        )

    def _get_annotation_polling(self, annotation: int) -> dict:
        annotation_object = self.get_annotation(annotation)
        status = annotation_object["status"]
        annotation_path = annotation_object["url"]
        if status == "importing":
            click.echo(".", nl=False, err=True)
            sys.stdout.flush()
        elif status == "to_review":
            click.echo(f"Processing of the annotation at {annotation_path} finished.", err=True)
        elif status == "failed_import":
            click.echo(" Processing failed.")
        return annotation_object

    def create_workspace(
        self, name: str, organization: str, metadata: Optional[Dict] = None
    ) -> Dict[str, Any]:
        data = {"name": name, "organization": organization}  # type: Dict[str, Any]
        if metadata:
            data.update({"metadata": metadata})
        return get_json(self.post("workspaces", data))

    def create_schema(self, name: str, content: List[dict]) -> dict:
        return get_json(self.post(SCHEMAS, data={"name": name, "content": content}))

    def create_queue(
        self,
        name: str,
        workspace_url: str,
        schema_url: str,
        connector_url: Optional[str] = None,
        hooks_urls: Optional[List] = None,
        locale: Optional[str] = None,
        rir_url: str = "https://all.rir.rossum.ai",
        rir_params: str = "",
    ) -> dict:
        data = {
            "name": name,
            "workspace": workspace_url,
            "schema": schema_url,
            "rir_url": rir_url,
            "rir_params": rir_params,
        }
        if connector_url is not None:
            data[CONNECTORS.singular] = connector_url
        if hooks_urls is not None:
            data[HOOKS.plural] = hooks_urls  # type: ignore
        if locale is not None:
            data["locale"] = locale
        return get_json(self.post("queues", data))

    def create_inbox(
        self, name: str, email_prefix: Optional[str], bounce_email: Optional[str], queue_url: str
    ) -> dict:
        if not (email_prefix and bounce_email):
            raise RossumException(
                "Inbox cannot be created without both bounce email and email prefix specified."
            )

        return get_json(
            self.post(
                "inboxes",
                data={
                    "name": name,
                    "email_prefix": email_prefix,
                    "bounce_email_to": bounce_email,
                    "bounce_unprocessable_attachments": True,
                    "queues": [queue_url],
                },
            )
        )

    def create_user(
        self,
        username: str,
        organization: str,
        queues: List[str],
        password: str,
        group: str,
        locale: str,
    ) -> dict:
        return get_json(
            self.post(
                USERS,
                data={
                    "username": username,
                    "email": username,
                    "organization": organization,
                    "password": password,
                    "groups": [g["url"] for g in self.get_groups(group_name=group)],
                    "queues": queues,
                    "ui_settings": {"locale": locale},
                },
            )
        )

    def change_user_password(self, new_password: str) -> dict:
        data = {
            "new_password1": new_password,
            "new_password2": new_password,
            "old_password": self.password,
        }
        return get_json(self.post("auth/password/change", data, expected_status_code=200))

    def reset_user_password(self, email: str) -> dict:
        data = {"email": email}
        return get_json(self.post("auth/password/reset", data, expected_status_code=200))

    def create_connector(
        self,
        name: str,
        queues: List[str],
        service_url: str,
        authorization_token: str = None,
        params: Optional[str] = None,
        asynchronous: Optional[bool] = True,
    ) -> dict:
        data = {
            "name": name,
            "queues": queues,
            "service_url": service_url,
            "authorization_token": authorization_token,
            "params": params,
            "asynchronous": asynchronous,
        }
        return get_json(self.post("connectors", data))

    def create_hook(
        self,
        name: str,
        hook_type: str,
        queues: List[str],
        active: bool,
        events: List[str],
        sideload: List[str],
        config: Dict,
    ) -> dict:

        data = {
            "name": name,
            "type": hook_type,
            "queues": queues,
            "active": active,
            "events": events,
            "sideload": sideload,
            "config": config,
        }
        return get_json(self.post("hooks", data))

    def upload_document(
        self, id_: int, file: str, filename_overwrite: str = "", values: Dict[str, str] = None
    ) -> dict:
        filename = PurePath(filename_overwrite).name or PurePath(file).name
        files: RequestsFiles = {"content": (filename, open(f"{file}", "rb"))}
        if values is not None:
            files["values"] = (None, json.dumps(values))
        return get_json(self.post(f"queues/{id_}/upload", files=files))

    def set_metadata(self, object_type: APIObject, object_id: int, metadata: Dict[str, Any]):
        return get_json(self.patch(f"{object_type}/{object_id}", {"metadata": metadata}))

    def export_data(self, id_: int, annotation_ids: Iterable[int], format_: str) -> Response:
        ids = ",".join(str(a) for a in annotation_ids)
        return self.get(f"queues/{id_}/export", query={"id": ids, "format": format_})

    def get_schemas(self, sideloads: Optional[Iterable[APIObject]] = None) -> List[dict]:
        schemas_list, _ = self.get_paginated(SCHEMAS)
        self._sideload(schemas_list, sideloads)
        return schemas_list


def get_json(response: Response) -> dict:
    try:
        return response.json()
    except ValueError as e:
        raise RossumException(f"Invalid JSON [{response.url}]: {response.text}") from e


def get_text(response: Response) -> str:
    try:
        return response.text
    except ValueError as e:
        raise RossumException(f"Invalid text [{response.url}]: {response.text}") from e


def _encode_booleans(query: Optional[dict]) -> Optional[dict]:
    if query is None:
        return query

    def bool_to_str(b: Any) -> Any:
        if isinstance(b, bool):
            return str(b).lower()
        return b

    res = {}
    for k, vs in query.items():
        if isinstance(vs, str) or not hasattr(vs, "__iter__"):
            vs = [vs]
        res[k] = (bool_to_str(v) for v in vs)
    return res
