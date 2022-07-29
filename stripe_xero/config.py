import os

# subscriptions start/end dates
START_DATE = os.environ.get("START_DATE") or "2021-05-01"
END_DATE = os.environ.get("END_DATE") or "2022-01-01"
# maximum invoices to process per batch
MAX_INVOICE_COUNT = 600


def check_configs():
    keys = [
        "STRIPE_SK",
        "XERO_TENANT_ID",
        "XERO_CLIENT_ID",
        "XERO_ACCOUNT_STRIPE_SALES",
        "XERO_ACCOUNT_STRIPE_FEES",
        "XERO_ACCOUNT_STRIPE_PAYMENTS",
    ]
    for key in keys:
        if not os.getenv(key):
            raise Exception(f"Please configure ${key} in the environment")
