import json
import os
from datetime import datetime

from localstack.utils.files import load_file

STATE_FILE = os.path.realpath("migration.state.json")

# subscriptions start/end dates
START_DATE = os.environ.get("START_DATE") or "2022-01-01"
END_DATE = os.environ.get("END_DATE") or "2022-12-31"
# maximum no of entities to process per batch
MAX_ENTITIES_COUNT = 600


def check_configs():
    keys = [
        "STRIPE_SK",
        "XERO_TENANT_ID",
        "XERO_CLIENT_ID",
        "XERO_CLIENT_SECRET",
        "XERO_ACCOUNT_STRIPE_SALES",
        "XERO_ACCOUNT_STRIPE_FEES",
        "XERO_ACCOUNT_STRIPE_PAYMENTS",
    ]
    for key in keys:
        if not os.getenv(key):
            raise Exception(f"Please configure ${key} in the environment")


def get_creation_timeframe(state=None):
    state = state or load_state_file()
    end_epoch = (
        to_epoch(END_DATE) if not state.get("last_date") else state["last_date"] + 60 * 60 * 24
    )
    kwargs = {"created": {"gt": to_epoch(START_DATE), "lt": end_epoch}}
    return kwargs


def to_epoch(date_str: str) -> int:
    return int((datetime.strptime(date_str, "%Y-%m-%d") - datetime(1970, 1, 1)).total_seconds())


def load_state_file():
    return json.loads(load_file(STATE_FILE) or "{}")
