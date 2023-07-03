import json
import os
import time
from datetime import datetime

from localstack.utils.files import load_file

STATE_FILE = os.path.realpath("migration.state.json")

# subscriptions start/end dates
START_DATE = os.environ.get("START_DATE") or "2023-01-01"
END_DATE = os.environ.get("END_DATE") or "2023-12-31"
# maximum no of entities to process per batch
MAX_ENTITIES_COUNT = 500

# whether to create fee invoices and payments (can be disabled, to reduce amount of created entities)
CREATE_FEES = False


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
    """
    Get timeframe of invoices to search (based on their creation timestamp). Note that the Stripe client
    iterates subscriptions from newest to oldest, hence keeping the `last_date` watermark as upper date limit
    """
    state = state or load_state_file()
    end_epoch = to_epoch(END_DATE)
    if state.get("last_date"):
        end_epoch = state["last_date"] + 60 * 60 * 24
    end_epoch = min(end_epoch, to_epoch(END_DATE))
    start_epoch = to_epoch(START_DATE)
    kwargs = {"created": {"gt": start_epoch, "lt": end_epoch}}
    return kwargs


def to_epoch(date_str: str) -> int:
    return int((datetime.strptime(date_str, "%Y-%m-%d") - datetime(1970, 1, 1)).total_seconds())


def from_epoch(date_int: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(date_int))


def load_state_file():
    return json.loads(load_file(STATE_FILE) or "{}")


# TODO remove? may not be required...
def get_currency_rate(date, currency):
    """Return the FX rate for the given date and currency, against the default base currency"""
    if not currency or str(currency).lower() == "usd":
        return 1.0
    # based on: https://www.x-rates.com/average/?from=EUR&to=USD&amount=1&year=2021
    eur_usd_rates = {
        # 2021
        "2021-01": 1.216983,
        "2021-02": 1.209595,
        "2021-03": 1.191048,
        "2021-04": 1.195110,
        "2021-05": 1.213948,
        "2021-06": 1.204671,
        "2021-07": 1.182689,
        "2021-08": 1.177138,
        "2021-09": 1.177812,
        "2021-10": 1.159816,
        "2021-11": 1.141091,
        "2021-12": 1.130427,
        # 2022
        "2022-01": 1.132515,
        "2022-02": 1.134099,
        "2022-03": 1.101000,
        "2022-04": 1.083068,
        "2022-05": 1.056852,
        "2022-06": 1.057404,
        "2022-07": 1.020338,
        "2022-08": 1.012215,
        "2022-09": 0.991832,
        "2022-10": 0.983173,
        "2022-11": 0.988636,
    }
    if isinstance(date, (int, float)):
        date = datetime.fromtimestamp(date)
    month = date.strftime("%Y-%m")
    result = eur_usd_rates.get(month)
    return result or 1.0
