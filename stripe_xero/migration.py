"""
Script to migrate customer data from Stripe into Xero (accounting software).
"""

import json
import logging
import time

from localstack.utils.common import save_file, timestamp

from stripe_xero.config import (
    check_configs,
    START_DATE,
    MAX_ENTITIES_COUNT,
    load_state_file,
    get_creation_timeframe,
    STATE_FILE,
    ONLY_PAID_INVOICES,
)
from stripe_xero.utils import dry_run, init_stripe, date_to_str
from stripe_xero.xero import XeroClient
from stripe_xero import stripe

LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
logging.getLogger("stripe").setLevel(logging.WARNING)


def get_client() -> XeroClient:
    # return QuickBooksClient()
    return XeroClient()


def create_invoices():
    client = get_client()

    count = 0
    state = load_state_file()
    migrated_invoices = state.setdefault("migrated", [])

    kwargs = get_creation_timeframe(state)

    for invoice in stripe.get_invoices(auto_paging=True, **kwargs):
        invoice_date = getattr(invoice, "date", None) or invoice.created
        state["last_date"] = invoice_date
        save_file(STATE_FILE, json.dumps(state))
        if not dry_run() and invoice["id"] in migrated_invoices:
            LOG.info(
                f"Invoice {invoice['id']} ({date_to_str(invoice_date)}) already migrated - skipping"
            )
            continue

        create_invoice(invoice, client=client)

        if not dry_run():
            migrated_invoices.append(invoice["id"])
            save_file(STATE_FILE, json.dumps(state))

        time.sleep(3)  # TODO: use better approach to deal with rate limiting
        if count >= MAX_ENTITIES_COUNT:
            print("Done.")
            return


def create_invoice(invoice, client=None):
    client = client or get_client()

    if isinstance(invoice, str):
        invoice = stripe.find_invoice(f"number: '{invoice}'")
        assert invoice

    paid = invoice.get("paid") and invoice.get("status") == "paid"
    date = timestamp(time=invoice.get("created"), format="%Y-%m-%d")
    if date < START_DATE:
        return
    if invoice.get("total", 0) <= 0:
        return
    if not paid and ONLY_PAID_INVOICES:
        return

    # fetch Stripe fees for this invoice
    invoice.fee = stripe.get_fees(invoice)
    if invoice.fee:
        invoice.payment_currency = invoice.fee["currency"]

    # get or create customer
    customer = {"id": invoice["customer"]}
    customer1 = client.get_customer(customer)
    if not customer1:
        customer = stripe.get_customer(invoice["customer"])
        client.create_customer(customer)

    # store invoice to accounting system
    client.create_customer_invoice(invoice)


def create_refunds():
    client = get_client()

    count = 0
    state = load_state_file()
    migrated_refunds = state.setdefault("migrated_refunds", [])

    kwargs = get_creation_timeframe(state)

    for refund in stripe.get_refunds(auto_paging=True, **kwargs):
        if not dry_run() and refund["id"] in migrated_refunds:
            LOG.info(
                f"Refund {refund['id']} ({date_to_str(refund['created'])}) already migrated - skipping"
            )
            continue

        # retrieve details of refunded charge
        refund["charge"] = stripe.get_charge(refund["charge"])
        refund["customer"] = refund["charge"]["customer"]
        refund["invoice"] = refund["charge"]["invoice"]

        # store refund in accounting system
        client.create_customer_refund(refund)

        if not dry_run():
            migrated_refunds.append(refund["id"])
            save_file(STATE_FILE, json.dumps(state))

        time.sleep(2)  # TODO: use better approach to deal with rate limiting
        if count >= MAX_ENTITIES_COUNT:
            print("Done.")
            return


def main():
    check_configs()
    init_stripe()

    # TODO: create invoices with the date of the payment date!
    # TODO: create a single invoice for all combined Stripe fees (instead of individual ones)

    # uncomment to create invoices
    # create_invoices()

    # uncomment to create refunds
    # create_refunds()

    # fix_payment_currencies()


if __name__ == "__main__":
    main()
