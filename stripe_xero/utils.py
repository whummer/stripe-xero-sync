import json
import os
import queue
import sys
import tempfile
import time
from datetime import datetime

from localstack.utils.numbers import is_number

import stripe as stripe_sdk
from localstack import config as localstack_config
from localstack.services.generic_proxy import ProxyListener, start_proxy_server
from localstack.utils.files import load_file

TOKEN_TMP_FILE = os.path.join(tempfile.gettempdir(), "tmp.token.json")

# login redirect endpoint
redirect_port = 54071
REDIRECT_URL = f"https://localhost.localstack.cloud:{redirect_port}/callback"
localstack_config.DISABLE_CORS_CHECKS = True


def dry_run():
    return "--dry-run" in sys.argv or "--dry" in sys.argv


def dry_run_prefix():
    return "DRYRUN:" if dry_run() else "!LIVE RUN:"


class BaseClient:
    def client(self):
        self._client = getattr(self, "_client", None)
        if self._client:
            return self._client

        class Listener(ProxyListener):
            def forward_request(self, method, path, *args, **kwargs):
                _queue.put(path)
                return {}

        _queue = queue.Queue()
        server = start_proxy_server(port=redirect_port, update_listener=Listener(), use_ssl=True)
        self._client = self._get_client(_queue)
        server.stop()
        return self._client

    def _get_client(self, result_queue):
        raise NotImplementedError

    def _cached_token(self):
        if not os.path.exists(TOKEN_TMP_FILE):
            return
        mod_time = os.path.getmtime(TOKEN_TMP_FILE)
        time_now = time.time()
        cache_duration_secs = 60 * 25
        if mod_time < (time_now - cache_duration_secs):
            return
        return json.loads(load_file(TOKEN_TMP_FILE))


def log(message):
    print(f"{dry_run_prefix()} {message}")


def date_to_str(date):
    if not is_number(date):
        return date
    return datetime.fromtimestamp(date)


def init_stripe():
    # set API key and endpoint from environment config
    stripe_sk = os.getenv("STRIPE_SK")
    if not stripe_sk:
        raise Exception("Please configure $STRIPE_SK in the environment")
    stripe_sdk.api_key = stripe_sk
