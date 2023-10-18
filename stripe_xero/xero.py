import json
import logging
import os
import uuid
from decimal import Decimal
from urllib.parse import parse_qs

from localstack.utils.files import save_file
from localstack.utils.strings import short_uid, to_str
from xero_python.accounting import (
    Account,
    Allocation,
    AccountingApi,
    Address,
    Contact,
    Contacts,
    CurrencyCode,
    CreditNotes,
    CreditNote,
    Invoice,
    Invoices,
    LineItem,
    Payment,
    Phone,
)
from xero_python.api_client import ApiClient, Configuration
from xero_python.api_client.oauth2 import OAuth2Token, TokenApi
from xero_python.identity import IdentityApi

from stripe_xero import config
from stripe_xero.config import get_creation_timeframe
from stripe_xero.utils import (
    REDIRECT_URL,
    TOKEN_TMP_FILE,
    BaseClient,
    dry_run,
    log,
    date_to_str,
)

LOG = logging.getLogger(__name__)

FORMAT_INVOICE_NO = "Stripe fee {invoice_number}"

# Xero configs
XERO_TENANT_ID = os.environ["XERO_TENANT_ID"]
XERO_CLIENT_ID = os.environ["XERO_CLIENT_ID"]
XERO_CLIENT_SECRET = os.environ["XERO_CLIENT_SECRET"]
XERO_STRIPE_CONTACT_ID = os.environ["XERO_STRIPE_CONTACT_ID"]
# account for receiving revenues from Stripe subscription invoices
XERO_ACCOUNT_STRIPE_SALES = os.environ["XERO_ACCOUNT_STRIPE_SALES"]
# account for Stripe fees to be paid per subscription invoice
XERO_ACCOUNT_STRIPE_FEES = os.environ["XERO_ACCOUNT_STRIPE_FEES"]
# account used for actual payments (revenues in, and fees out)
XERO_ACCOUNT_STRIPE_PAYMENTS = os.environ["XERO_ACCOUNT_STRIPE_PAYMENTS"]
# codes for tax rates
INVOICE_TAX_RATE_CH = "OUTPUT"  # "UN77"
# INVOICE_TAX_RATE_CH = "TAX010"  # "ULA"
INVOICE_TAX_RATE_OTHER = "TAX010"  # "ULA"
FEES_TAX_RATE = "NONE"  # "Tax Exempt"

CACHE = {}


class XeroClient(BaseClient):
    """Client for xero.com"""

    def customers(self):
        accounting_api = AccountingApi(self.client())
        # TODO: requires paging?
        return accounting_api.get_contacts(self._tenant()).contacts

    def get_customer(self, customer):
        customers = CACHE.get("customers")
        if not customers:
            CACHE["customers"] = customers = self.customers()
        customer_id = customer if isinstance(customer, str) else customer["id"]

        def _matches(c):
            return str(c.last_name).endswith(f"({customer_id})")

        existing = [c for c in customers if _matches(c)]
        return (existing or [None])[0]

    def create_customer(self, data):
        accounting_api = AccountingApi(self.client())
        contacts = Contacts()
        address = data.get("address") or {}
        addr = Address(
            address_type="STREET",
            address_line1=address.get("line1"),
            address_line2=address.get("line2"),
            city=address.get("city"),
            region=address.get("state"),
            postal_code=address.get("postal_code"),
            country=address.get("country"),
        )
        phone = Phone(phone_number=data.get("phone"))

        # Hacks:
        #  - using lastname field to store Stripe customer ID for now
        #  - storing email as firstname, to avoid sending out emails to customers!
        # Contacts do not seem to have a generic description field; also, the "website" field is not
        # getting stored on creation. We could consider using the "attachments" fields in the future.
        lastname = f"{data.get('firstname') or 'undefined'} ({data['id']})"
        firstname = data["email"]

        contact = Contact(
            name=data["name"],
            first_name=firstname,
            last_name=lastname,
            # email_address=None,  # skip setting email! (avoid sending out messages)
            addresses=[addr],
            phones=[phone],
            account_number=data["id"],
            is_customer=True,
        )
        log(f"Creating contact '{data['name']}' - {data['email']} - {data['id']}")
        if dry_run():
            CACHE.setdefault("customers", []).append(contact)
            contact.contact_id = str(uuid.uuid4())
            return contact

        contacts.contacts = [contact]
        try:
            result = accounting_api.create_contacts(self._tenant(), contacts)
        except Exception as e:
            if "contact name must be unique" not in str(e):
                raise
            # fall back to appending the ID to the contact name
            contact.name = f"{contact.name} ({data['id']})"
            result = accounting_api.create_contacts(self._tenant(), contacts)
        contact = result.contacts[0]
        CACHE.setdefault("customers", []).append(contact)
        return contact

    def get_or_create_customer(self, customer):
        LOG.debug("Creating customer:", customer["name"])
        existing = self.get_customer(customer)
        if existing:
            LOG.debug(f"Customer '{customer['name']}' already exists, skipping ...")
            return existing
        return self.create_customer(customer)

    def create_invoice(
        self,
        acc_code,
        contact,
        description,
        total,
        currency,
        lines,
        date,
        reference=None,
        due_date=None,
        paid_at=None,
        url=None,
        invoice_no=None,
    ):
        accounting_api = AccountingApi(self.client())
        account_type = "ACCREC" if acc_code == XERO_ACCOUNT_STRIPE_SALES else "ACCPAY"

        # determine invoice tax type based on customer country
        tax_type = self._get_customer_tax_type(contact)

        line_items = []
        for line in lines:
            # ensure correct param combinations, to avoid "the line total xx does not match the expected line total yy"
            line_amount = None
            unit_amount = None
            quantity = None
            is_fee_invoice = not hasattr(line, "price")
            if is_fee_invoice or line.amount != line.quantity * (line.price.unit_amount or 0):
                # set a custom line amount if the `amount` does not match `quantity` * `unit_amount`
                # (use None if `unit_amount` is present (per-seat billing), or use `amount` for metered billing)
                line_amount = line.amount / 100
            if is_fee_invoice or (not line_amount and line.unit_amount):
                unit_amount = line.unit_amount / 100
                quantity = line.quantity or 1
                line_amount = unit_amount * quantity
            if not unit_amount and not line_amount:
                # skip recording empty line items (with amount 0)
                continue
            if getattr(line, "discount_rate", 0):
                line_amount *= (100 - line.discount_rate) / 100
            item = LineItem(
                description=line.description or description,
                quantity=quantity,
                unit_amount=unit_amount,
                line_amount=line_amount,
                account_code=acc_code,
                tax_type=getattr(line, "tax_type", None) or tax_type,
                discount_rate=getattr(line, "discount_rate", None),
            )
            line_items.append(item)

        contact = Contact(contact_id=contact) if isinstance(contact, str) else contact

        invoice = Invoice(
            reference=reference,
            type=account_type,
            contact=contact,
            line_items=line_items,
            date=self._convert_date(date),
            due_date=self._convert_date(due_date or date),
            invoice_number=invoice_no,
            url=url,
            currency_code=CurrencyCode(currency.upper()),
            status="AUTHORISED",
            total=total,
            amount_paid=total if paid_at else None,
            fully_paid_on_date=paid_at,
        )

        log(
            f"Creating invoice '{description}', {total} {currency} for customer {contact.contact_id}"
        )
        if dry_run():
            invoice.invoice_id = str(uuid.uuid4())
            return invoice

        invoices = Invoices([invoice])
        try:
            result = accounting_api.create_invoices(self._tenant(), invoices=invoices).invoices
        except Exception as e:
            if "Invoice # must be unique" not in str(e):
                raise
            invoice.invoice_number = f"{invoice.invoice_number}-{short_uid()}"
            result = accounting_api.create_invoices(self._tenant(), invoices=invoices).invoices
        invoice = result[0]
        return invoice

    def _get_customer_tax_type(self, contact) -> str:
        addresses = getattr(contact, "addresses", None)
        if addresses:
            address = addresses[0]
            country = address.country
            if country == "CH":
                return INVOICE_TAX_RATE_CH
        return INVOICE_TAX_RATE_OTHER

    def create_payment(self, invoice_or_credit_note, account):
        """Create a payment of an invoice or credit note (refund) to a given account"""
        accounting_api = AccountingApi(self.client())

        payment = Payment(
            amount=invoice_or_credit_note.total,
            account=Account(code=account),
            date=invoice_or_credit_note.date,
            currency_rate=invoice_or_credit_note.currency_rate,
        )

        invoice_id = getattr(invoice_or_credit_note, "invoice_id", None)
        if invoice_id:
            entity_id = invoice_id
            entity_type = "invoice"
            payment.invoice = invoice_or_credit_note
            payment.payment_type = "ACCRECPAYMENT"
        else:
            entity_id = invoice_or_credit_note.credit_note_id
            entity_type = "credit note (refund)"
            payment.credit_note = invoice_or_credit_note
            payment.payment_type = "ARCREDITPAYMENT"

        log(
            f"Creating payment of {invoice_or_credit_note.total} {invoice_or_credit_note.currency_code.value} "
            f"on {invoice_or_credit_note.date} for {entity_type} {entity_id} to account {account}"
        )
        if dry_run():
            return payment
        result = accounting_api.create_payment(self._tenant(), payment)
        return result

    def get_existing_customer_invoice(self, data):
        accounting_api = AccountingApi(self.client())
        if hasattr(data, "customer") and data.object == "invoice":
            invoice_no = data.id
            customer = data.customer
        else:
            invoice_no = data["invoice"]
            customer = data["customer"]

        contact = self.get_customer(customer)
        if not contact:
            LOG.warning("Unable to find customer %s in Xero: %s", customer, contact)
            return

        # check if invoice with this ID already exists
        existing = accounting_api.get_invoices(self._tenant(), contact_i_ds=[contact.contact_id])
        existing = [
            inv
            for inv in existing.invoices
            if inv.status != "VOIDED"
            and (invoice_no in inv.invoice_number or invoice_no in inv.reference)
        ]
        if existing:
            return existing[0]

    def get_existing_invoice_fee(self, invoice):
        accounting_api = AccountingApi(self.client())
        invoice_no = FORMAT_INVOICE_NO.format(invoice_number=invoice.invoice_number)
        existing = accounting_api.get_invoices(self._tenant(), invoice_numbers=[invoice_no])
        if existing and existing.invoices:
            return existing.invoices[0]

    def get_existing_refund(self, data):
        accounting_api = AccountingApi(self.client())
        existing = accounting_api.get_credit_notes(self._tenant())
        existing = [cn for cn in existing.credit_notes if data.id in str(cn.credit_note_number)]
        if existing:
            return existing[0]

    def delete_invoice_payment(self, invoice):
        assert len(invoice.payments) == 1
        payment_id = invoice.payments[0].payment_id
        accounting_api = AccountingApi(self.client())
        result = accounting_api.delete_payment(
            self._tenant(), payment_id=payment_id, payment_delete={"Status": "DELETED"}
        )
        return result

    def update_invoice(self, invoice):
        accounting_api = AccountingApi(self.client())
        invoices = Invoices([invoice])
        result = accounting_api.update_invoice(
            self._tenant(), invoice_id=invoice.invoice_id, invoices=invoices
        )
        return result.invoices[0]

    def create_refund_credit_note(self, data):
        accounting_api = AccountingApi(self.client())

        invoice = self.get_existing_customer_invoice(data)
        if not invoice:
            LOG.warning("Unable to find subscription invoice: %s", data.invoice)
            return
        contact = self.get_customer(data["customer"])

        # create allocation and line item
        allocation = Allocation(
            amount=data["amount"] / 100,
            invoice=invoice,
            date=self._convert_date(data["created"]),
        )
        amount = data["amount"] / 100
        acc_code = XERO_ACCOUNT_STRIPE_SALES
        tax_type = self._get_customer_tax_type(contact)
        line_item = LineItem(
            line_item_id="",
            quantity=1,
            unit_amount=amount,
            line_amount=amount,
            account_code=acc_code,
            tax_type=tax_type,
            description=f"Stripe refund {data.id} for invoice {invoice.invoice_number}",
        )
        line_item.amount = amount

        # create credit note entity
        credit_note = CreditNote(
            credit_note_number=f"Stripe refund {data.id}",
            contact=contact,
            date=self._convert_date(data["created"]),
            reference=data["charge"]["invoice"],
            total=data["amount"] / 100,
            sub_total=data["amount"] / 100,
            currency_code=CurrencyCode(data["currency"].upper()),
            line_items=[line_item],
            allocations=[allocation],
            type="ACCRECCREDIT",
            status="AUTHORISED",
        )

        log(
            f"Creating refund credit note {data.id} for "
            f"invoice {data['charge']['invoice']}, customer {data['customer']}"
        )
        if dry_run():
            return credit_note

        credit_notes = CreditNotes([credit_note])
        result = accounting_api.create_credit_notes(self._tenant(), credit_notes)
        credit_note = result.credit_notes[0]

        # create payment for credit note
        self.create_payment(credit_note, XERO_ACCOUNT_STRIPE_PAYMENTS)

        return result

    def create_customer_invoice(self, data):

        # check if invoice with this ID already exists
        existing = self.get_existing_customer_invoice(data)
        if existing:
            return existing

        contact = self.get_customer(data.customer)

        # prepare lines with unit amount and discounts (if any)
        lines = data.lines["data"]
        for line in lines:
            line.unit_amount = line.price.unit_amount
            discount_rate = self._discount_rate(line)
            if discount_rate:
                line.discount_rate = discount_rate

        plan = data.get("plan") or data["lines"]["data"][0]["plan"]["nickname"]
        invoice = self.create_invoice(
            acc_code=XERO_ACCOUNT_STRIPE_SALES,
            reference=f"Stripe invoice {data['id']}",
            contact=contact,
            description=f'{plan} ({data["number"]})',
            total=data["total"] / 100,
            currency=data["currency"],
            lines=lines,
            date=data.get("date") or data["created"],
            due_date=data["due_date"],
            url=data["hosted_invoice_url"],
            # note: data["number"] is the human-readable invoice no shared with the customer
            invoice_no=data["number"],
        )

        # create payment for invoice
        if hasattr(data, "payment_currency"):
            invoice.currency_code = CurrencyCode(data.payment_currency.upper())
        self.create_payment(invoice, XERO_ACCOUNT_STRIPE_PAYMENTS)

        # create fee payment for invoice
        if (data.get("fee") or {}).get("fee"):
            self.create_fee_bill_invoice(data, invoice)

        return invoice

    def create_customer_refund(self, data):
        # check if invoice with this ID already exists
        existing = self.get_existing_refund(data)
        if existing:
            return existing

        self.create_refund_credit_note(data)

    def create_fee_bill_invoice(self, data, invoice):
        if not config.CREATE_FEES:
            return
        fee = data["fee"]
        line_item = LineItem(
            line_item_id="",
            quantity=1,
            unit_amount=fee["fee"],
            line_amount=fee["fee"],
            tax_type=FEES_TAX_RATE,
        )
        line_item.amount = fee["fee"]

        fee_invoice = self.create_invoice(
            acc_code=XERO_ACCOUNT_STRIPE_FEES,
            reference=FORMAT_INVOICE_NO.format(invoice_number=fee.get("id")),
            contact=XERO_STRIPE_CONTACT_ID,
            description=f"Stripe fee for invoice {invoice.invoice_number}",
            total=fee["fee"] / 100,
            currency=fee["currency"],
            date=invoice.date,
            lines=[line_item],
            invoice_no=FORMAT_INVOICE_NO.format(invoice_number=invoice.invoice_number),
            paid_at=invoice.date,
        )
        self.create_payment(fee_invoice, XERO_ACCOUNT_STRIPE_PAYMENTS)

    # util functions below

    def _tenant(self):
        if XERO_TENANT_ID:
            return XERO_TENANT_ID
        id_api = IdentityApi(self.client())
        result = id_api.get_connections()
        if len(result) > 1:
            raise Exception(f"More than one tenant found: {result}")
        return result[0].tenant_id

    def _discount_rate(self, line_item) -> float:
        if not line_item.discount_amounts or not line_item.amount:
            return 0
        discount = sum([dis.amount for dis in line_item.discount_amounts]) / line_item.amount * 100
        return discount

    def _convert_date(self, date):
        return date_to_str(date)

    def _get_client(self, result_queue):

        api_client = ApiClient(
            Configuration(
                # debug=True,
                oauth2_token=OAuth2Token(
                    client_id=XERO_CLIENT_ID, client_secret=XERO_CLIENT_SECRET
                ),
            ),
        )

        self.session = {}

        @api_client.oauth2_token_getter
        def obtain_xero_oauth2_token():
            return self.session.get("token")

        @api_client.oauth2_token_saver
        def store_xero_oauth2_token(token):
            self.session["token"] = token

        # get client token
        token_data = self._token(api_client, result_queue)
        store_xero_oauth2_token(token_data)

        return api_client

    def _token(self, api_client, result_queue):
        token = self._cached_token()
        if token:
            return token

        scopes = [
            "accounting.transactions",
            "accounting.settings",
            "accounting.contacts",
            "projects",
            "assets",
        ]
        url = (
            f"https://login.xero.com/identity/connect/authorize?response_type=code&"
            f"client_id={XERO_CLIENT_ID}&redirect_uri={REDIRECT_URL}&"
            f"scope=openid+profile+email+{'+'.join(scopes)}"
        )

        print(f"Open this URL in your browser:\n{url}")

        # retrieve auth result asynchronously from callback
        result = result_queue.get()
        result = parse_qs(result.partition("?")[2])

        code = result.get("code")[0]

        post_data = {
            "client_id": XERO_CLIENT_ID,
            "client_secret": XERO_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URL,
        }
        response, status, headers = api_client.call_api(
            TokenApi.client_credentials_token_url,
            "POST",
            header_params={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            post_params=post_data,
            _preload_content=False,
        )
        token_data = json.loads(to_str(response.data))
        save_file(TOKEN_TMP_FILE, json.dumps(token_data))
        return token_data

    # misc. temporary util functions below

    def fix_discount_for_invoice(self, stripe_invoice, xero_invoice):

        if not xero_invoice.payments:
            return

        accounting_api = AccountingApi(self.client())

        # delete payments
        for payment in xero_invoice.payments:
            accounting_api.delete_payment(self._tenant(), payment.payment_id, {"Status": "DELETED"})
        xero_invoice.payments = []

        # update discount in line item in invoice
        stripe_discount = self._discount_rate(stripe_invoice.lines.data[0])
        xero_invoice.line_items[0].discount_rate = stripe_discount
        discount_factor = Decimal((100 - stripe_discount) / 100)
        xero_invoice.line_items[0].line_amount = (
            xero_invoice.line_items[0].line_amount * discount_factor
        )
        xero_invoice.total = xero_invoice.total * discount_factor
        xero_invoice.status = "AUTHORISED"
        accounting_api.update_invoice(self._tenant(), xero_invoice.invoice_id, xero_invoice)

        # re-create payment
        self.create_payment(xero_invoice, XERO_ACCOUNT_STRIPE_PAYMENTS)


def reconcile_invoice_discounts():
    """Temporary utility function to reconcile/fix invoice discounts between Stripe & Xero invoices"""
    from stripe_xero import stripe

    client = XeroClient()
    kwargs = get_creation_timeframe()
    for invoice in stripe.get_invoices(auto_paging=True, **kwargs):
        stripe_discounts = invoice.lines.data[0].discount_amounts
        if not stripe_discounts:
            continue

        try:
            xero_invoice = client.get_existing_customer_invoice(invoice)
        except Exception:
            continue
        if not xero_invoice:
            continue
        line_items = xero_invoice.line_items
        if not line_items:
            continue
        if len(line_items) > 1:
            LOG.warning("Multiple line items found for invoice %s", xero_invoice.id)
            continue
        discount_rate = line_items[0].discount_rate
        if discount_rate:
            continue
        client.fix_discount_for_invoice(invoice, xero_invoice)
