# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import json
import os
import shlex
from pprint import pprint
from shutil import copyfile
from time import sleep

import pytest
import requests
from python_on_whales import DockerClient, docker
from python_on_whales.exceptions import DockerException

# isort:off (needed to workaround isort bug)
from docker_tests.command_utils import run_command
from docker_tests.constants import AIRFLOW_ROOT_PATH

# isort:on (needed to workaround isort bug)

DOCKER_COMPOSE_HOST_PORT = os.environ.get("HOST_PORT", "localhost:8080")
AIRFLOW_WWW_USER_USERNAME = os.environ.get("_AIRFLOW_WWW_USER_USERNAME", "airflow")
AIRFLOW_WWW_USER_PASSWORD = os.environ.get("_AIRFLOW_WWW_USER_PASSWORD", "airflow")
DAG_ID = "example_bash_operator"
DAG_RUN_ID = "test_dag_run_id"


def get_jwt_token() -> str:
    """
    Get the JWT token.

    Note: API server is still using FAB Auth Manager.

    Steps:
    1. Get the login page to get the csrf token
        - The csrf token is in the hidden input field with id "csrf_token"
    2. Login with the username and password
        - Must use the same session to keep the csrf token session
    3. Extract the JWT token from the redirect url
        - Expected to have a connection error
        - The redirect url should have the JWT token as a query parameter

    :return: The JWT token
    """
    # get csrf token from login page
    session = requests.Session()
    url = f"http://{DOCKER_COMPOSE_HOST_PORT}/auth/token"
    login_response = session.post(
        url,
        json={
            "username": AIRFLOW_WWW_USER_USERNAME,
            "password": AIRFLOW_WWW_USER_PASSWORD,
        },
    )
    jwt_token = login_response.json().get("access_token")

    assert jwt_token, f"Failed to get JWT token from redirect url {url} with status code {login_response}"
    return jwt_token


def api_request(
    method: str, path: str, base_url: str = f"http://{DOCKER_COMPOSE_HOST_PORT}/api/v2", **kwargs
) -> dict:
    response = requests.request(
        method=method,
        url=f"{base_url}/{path}",
        headers={"Authorization": f"Bearer {get_jwt_token()}", "Content-Type": "application/json"},
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def wait_for_terminal_dag_state(dag_id, dag_run_id):
    print(f" Simplified representation of DAG {dag_id} ".center(72, "="))
    pprint(api_request("GET", f"dags/{DAG_ID}/details"))

    # Wait 400 seconds
    for _ in range(400):
        dag_state = api_request("GET", f"dags/{dag_id}/dagRuns/{dag_run_id}").get("state")
        print(f"Waiting for DAG Run: dag_state={dag_state}")
        sleep(1)
        if dag_state in ("success", "failed"):
            break


def test_trigger_dag_and_wait_for_result(default_docker_image, tmp_path_factory, monkeypatch):
    """Simple test which reproduce setup docker-compose environment and trigger example dag."""
    tmp_dir = tmp_path_factory.mktemp("airflow-quick-start")
    monkeypatch.setenv("AIRFLOW_IMAGE_NAME", default_docker_image)

    compose_file_path = (
        AIRFLOW_ROOT_PATH / "airflow-core" / "docs" / "howto" / "docker-compose" / "docker-compose.yaml"
    )
    copyfile(compose_file_path, tmp_dir / "docker-compose.yaml")

    # Create required directories for docker compose quick start howto
    for subdir in ("dags", "logs", "plugins"):
        (tmp_dir / subdir).mkdir()

    dot_env_file = tmp_dir / ".env"
    dot_env_file.write_text(f"AIRFLOW_UID={os.getuid()}\n")
    print(" .env file content ".center(72, "="))
    print(dot_env_file.read_text())

    compose_version = None
    try:
        compose_version = docker.compose.version()
    except DockerException:
        pytest.fail("`docker compose` not available. Make sure compose plugin is installed")
    try:
        docker_version = docker.version()
    except NotImplementedError:
        docker_version = run_command(["docker", "version"], return_output=True)

    compose = DockerClient(compose_project_name="quick-start", compose_project_directory=tmp_dir).compose
    compose.down(remove_orphans=True, volumes=True, quiet=True)
    try:
        compose.up(detach=True, wait=True, color=not os.environ.get("NO_COLOR"))
        # Before we proceed, let's make sure our DAG has been parsed
        compose.execute(service="airflow-dag-processor", command=["airflow", "dags", "reserialize"])

        api_request("PATCH", path=f"dags/{DAG_ID}", json={"is_paused": False})
        api_request(
            "POST",
            path=f"dags/{DAG_ID}/dagRuns",
            json={"dag_run_id": DAG_RUN_ID, "logical_date": "2020-06-11T18:00:00+00:00"},
        )

        wait_for_terminal_dag_state(dag_id=DAG_ID, dag_run_id=DAG_RUN_ID)
        dag_state = api_request("GET", f"dags/{DAG_ID}/dagRuns/{DAG_RUN_ID}").get("state")
        assert dag_state == "success"
    except Exception:
        print("HTTP: GET health")
        pprint(api_request("GET", "monitor/health"))
        print(f"HTTP: GET dags/{DAG_ID}/dagRuns")
        pprint(api_request("GET", f"dags/{DAG_ID}/dagRuns"))
        print(f"HTTP: GET dags/{DAG_ID}/dagRuns/{DAG_RUN_ID}/taskInstances")
        pprint(api_request("GET", f"dags/{DAG_ID}/dagRuns/{DAG_RUN_ID}/taskInstances"))
        print(" Docker Version ".center(72, "="))
        print(docker_version)
        print(" Docker Compose Version ".center(72, "="))
        print(compose_version)
        print(" Compose Config ".center(72, "="))
        print(json.dumps(compose.config(return_json=True), indent=4))

        for service in compose.ps(all=True):
            print(f" Service: {service.name} ".center(72, "-"))
            print(" Service State ".center(72, "."))
            pprint(service.state)
            print(" Service Config ".center(72, "."))
            pprint(service.config)
            print(" Service Logs ".center(72, "."))
            print(service.logs())
        raise
    finally:
        if not os.environ.get("SKIP_DOCKER_COMPOSE_DELETION"):
            compose.down(remove_orphans=True, volumes=True, quiet=True)
            print("Docker compose instance deleted")
        else:
            print("Skipping docker-compose deletion")
            print()
            print("You can run inspect your docker-compose by running commands starting with:")
            quoted_command = map(shlex.quote, map(str, compose.docker_compose_cmd))
            print(" ".join(quoted_command))
