"""
Simple script to migrate customer data from Stripe (and other sources) into QuickBooks or Xero (accounting software).
"""

import json
import logging
import time

from localstack.utils.common import load_file, save_file, timestamp

from stripe_xero.config import check_configs, END_DATE, START_DATE, MAX_INVOICE_COUNT
from stripe_xero.utils import STATE_FILE, dry_run, init_stripe, to_epoch
from stripe_xero.xero import XeroClient
from stripe_xero import stripe

LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
logging.getLogger("stripe").setLevel(logging.WARNING)


def get_client() -> XeroClient:
    # return QuickBooksClient()
    return XeroClient()


# def create_customers():
#     client = get_client()
#     for customer in stripe.list_customers():
#         client.get_or_create_customer(customer)


def create_invoices():
    client = get_client()

    count = 0
    state = json.loads(load_file(STATE_FILE) or "{}")
    migrated_invoices = state.setdefault("migrated", [])

    end_epoch = (
        to_epoch(END_DATE) if not state.get("last_date") else state["last_date"] + 60 * 60 * 12
    )
    kwargs = {"created": {"gt": to_epoch(START_DATE), "lt": end_epoch}}

    for invoice in stripe.get_invoices(auto_paging=True, **kwargs):
        state["last_date"] = invoice.date
        save_file(STATE_FILE, json.dumps(state))
        if not dry_run() and invoice["id"] in migrated_invoices:
            LOG.info(f"Invoice {invoice['id']} already migrated - skipping")
            continue

        count += 1
        paid = invoice.get("paid")
        date = timestamp(time=invoice.get("created"), format="%Y-%m-%d")
        if date < START_DATE:
            continue
        if invoice.get("total", 0) <= 0:
            continue
        if not paid:
            continue

        # fetch Stripe fees for this invoice
        invoice.fee = stripe.get_fees(invoice)

        # get or create customer
        customer = {"id": invoice["customer"]}
        customer1 = client.get_customer(customer)
        if not customer1:
            customer = stripe.get_customer(invoice["customer"])
            client.create_customer(customer)

        # store invoice to accounting system
        client.create_customer_invoice(invoice)

        if not dry_run():
            migrated_invoices.append(invoice["id"])
            save_file(STATE_FILE, json.dumps(state))

        time.sleep(2)  # TODO: better approach to deal with rate limiting
        if count >= MAX_INVOICE_COUNT:
            print("Done.")
            return


def main():
    check_configs()
    init_stripe()
    create_invoices()


if __name__ == "__main__":
    main()
