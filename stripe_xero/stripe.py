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
                invoice.plan = (
                    getattr(subscription.plan, "nickname", None) or subscription.plan.name
                )
            invoice.quantity = subscription.quantity
        return invoice

    if customer_id:
        kwargs["customer"] = customer_id
    entries = stripe_sdk.Invoice.list(**kwargs)
    if auto_paging:
        for entry in entries.auto_paging_iter():
            yield _format_invoice(entry)
        return

    entries = [_format_invoice(inv) for inv in entries.get("data", [])]
    return entries


def get_invoice(invoice_id: str = None) -> stripe_sdk.Invoice:
    result = stripe_sdk.Invoice.retrieve(invoice_id)
    return result


def find_invoice(query: str) -> Optional[stripe_sdk.Invoice]:
    result = stripe_sdk.Invoice.search(query=query)
    data = result.get("data") or []
    return (data or [None])[0]


def get_refunds(
    customer_id: str = None, auto_paging=False, **kwargs
) -> Iterable[stripe_sdk.Refund]:
    if customer_id:
        kwargs["customer"] = customer_id
    entries = stripe_sdk.Refund.list(**kwargs)
    if auto_paging:
        for entry in entries.auto_paging_iter():
            yield entry
        return
    entries = [ref for ref in entries.get("data", [])]
    return entries


def get_fees(invoice: stripe_sdk.Invoice) -> Optional[Fee]:
    transaction = get_fee_transaction(invoice)
    if not transaction:
        return
    result = Fee(**select_attributes(transaction, ["fee", "fee_details", "currency"]))
    return result


def get_fee_transaction(invoice: stripe_sdk.Invoice) -> Optional[stripe_sdk.BalanceTransaction]:
    if not invoice.charge:
        return
    charge = stripe_sdk.Charge.retrieve(invoice.charge)
    txn = charge.get("balance_transaction")
    if not txn:
        return
    transaction = stripe_sdk.BalanceTransaction.retrieve(txn)
    return transaction


def get_subscription(subscription_id) -> stripe_sdk.Subscription:
    if subscription_id not in CACHE:
        CACHE[subscription_id] = stripe_sdk.Subscription.retrieve(subscription_id)
    return CACHE[subscription_id]


def get_charge(charge_id) -> stripe_sdk.Charge:
    if charge_id not in CACHE:
        CACHE[charge_id] = stripe_sdk.Charge.retrieve(charge_id)
    return CACHE[charge_id]
