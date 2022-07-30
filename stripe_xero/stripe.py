from typing import Iterable, Dict, Optional, TypedDict, List

from localstack.utils.collections import select_attributes

from stripe_xero.utils import stripe_sdk


CACHE = {}


class Fee(TypedDict):
    fee: float
    currency: str
    details: Optional[List[Dict]]


def get_customer(customer_id: str) -> stripe_sdk.Customer:
    if customer_id not in CACHE:
        CACHE[customer_id] = stripe_sdk.Customer.retrieve(customer_id)
    return CACHE[customer_id]


def list_customers() -> Iterable[stripe_sdk.Customer]:
    return stripe_sdk.Customer.list()


def get_invoices(
    customer_id: str = None, auto_paging=False, **kwargs
) -> Iterable[stripe_sdk.Invoice]:
    def _format_invoice(invoice):
        invoice.plan = "Custom Invoice"
        if invoice.subscription:
            subscription = get_subscription(invoice.subscription)
            if subscription.plan:
                invoice.plan = subscription.plan.name
            invoice.quantity = subscription.quantity
        return invoice

    if customer_id:
        kwargs["customer"] = customer_id
    entries = stripe_sdk.Invoice.list(**kwargs)
    if auto_paging:
        for entry in entries.auto_paging_iter():
            yield _format_invoice(entry)
    else:
        entries = [_format_invoice(inv) for inv in entries.get("data", [])]

    return entries


def get_fees(invoice: stripe_sdk.Invoice) -> Optional[Fee]:
    if not invoice.charge:
        return
    charge = stripe_sdk.Charge.retrieve(invoice.charge)
    txn = charge.get("balance_transaction")
    if not txn:
        return
    transaction = stripe_sdk.BalanceTransaction.retrieve(txn)
    result = Fee(**select_attributes(transaction, ["fee", "fee_details", "currency"]))
    return result


def get_subscription(subscription_id) -> stripe_sdk.Subscription:
    if subscription_id not in CACHE:
        CACHE[subscription_id] = stripe_sdk.Subscription.retrieve(subscription_id)
    return CACHE[subscription_id]
