import json
import logging
import math
import os
import uuid
from datetime import datetime
from urllib.parse import parse_qs

from localstack.utils.files import save_file
from localstack.utils.numbers import is_number
from localstack.utils.strings import short_uid, to_str
from xero_python.accounting import (
    Account,
    AccountingApi,
    Address,
    Contact,
    Contacts,
    CurrencyCode,
    Invoice,
    Invoices,
    LineItem,
    Payment,
    Phone,
)
from xero_python.api_client import ApiClient, Configuration
from xero_python.api_client.oauth2 import OAuth2Token, TokenApi
from xero_python.identity import IdentityApi

from stripe_xero.utils import (
    REDIRECT_URL,
    TOKEN_TMP_FILE,
    BaseClient,
    dry_run,
    dry_run_prefix,
)

LOG = logging.getLogger(__name__)

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
INVOICE_TAX_RATE_OTHER = "TAX010"  # "ULA"

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
            is_customer=True,
        )
        print(
            f"{dry_run_prefix()} Creating contact '{data['name']}' - {data['email']} - {data['id']}"
        )
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

        def _fix_line(line):
            expected = (line.unit_amount or 0) * (line.quantity or 0) / 100
            actual = line.line_amount / 100
            if not math.isclose(expected, actual):
                LOG.debug(
                    f"Updating invoice line (amount {line.line_amount}): "
                    f"qty {line.quantity}->1, unit_amt {line.unit_amount}->{line.line_amount}"
                )
                line.quantity = 1
                line.unit_amount = line.line_amount
            return line

        # determine tax type based on customer country
        tax_type = INVOICE_TAX_RATE_OTHER
        addresses = getattr(contact, "addresses", None)
        if addresses:
            address = addresses[0]
            country = address.country
            if country == "CH":
                tax_type = INVOICE_TAX_RATE_CH

        line_items = [
            _fix_line(
                LineItem(
                    # TODO: use description of line items?
                    description=description,
                    quantity=line.quantity or 1,
                    unit_amount=line.unit_amount,
                    line_amount=line.amount / 100,
                    account_code=acc_code,
                    tax_type=tax_type,
                    # TODO
                    # discount_rate=None,
                    # discount_amount=None,
                )
            )
            for line in lines
        ]

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

        print(
            f"{dry_run_prefix()} Creating invoice '{description}', {total} {currency} for customer {contact.contact_id}"
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

    def create_payment(self, invoice, account):
        accounting_api = AccountingApi(self.client())
        invoice_payment = Payment(
            invoice=invoice,
            amount=invoice.total,
            account=Account(code=account),
            date=invoice.date,
        )
        print(
            f"{dry_run_prefix()} Creating payment of {invoice.total} {invoice.currency_code.value} "
            f"on {invoice.date} for invoice {invoice.invoice_id} to account {account}"
        )
        if dry_run():
            return invoice_payment
        accounting_api.create_payment(self._tenant(), invoice_payment)

    def create_customer_invoice(self, data):
        accounting_api = AccountingApi(self.client())
        contact = self.get_customer(data.customer)

        # check if invoice with this ID already exists
        existing = accounting_api.get_invoices(self._tenant(), contact_i_ds=[contact.contact_id])
        existing = [
            inv
            for inv in existing.invoices
            if inv.status != "VOIDED" and inv.invoice_number.startswith(data.id)
        ]
        if existing:
            return existing[0]

        # get payment date
        paid_at = data.status_transitions.paid_at if data.status_transitions else None
        if paid_at:
            paid_at = self._convert_date(paid_at)

        # print("!!data", data["lines"])
        lines = data["lines"]["data"]
        for line in lines:
            line.unit_amount = line.price.unit_amount
        invoice = self.create_invoice(
            acc_code=XERO_ACCOUNT_STRIPE_SALES,
            reference=f"Stripe invoice {data['id']}",
            contact=contact,
            description=data["plan"],
            total=data["total"],
            currency=data["currency"],
            lines=lines,
            date=data["date"],
            due_date=data["due_date"],
            url=data["hosted_invoice_url"],
            invoice_no=data["id"],
        )

        # create payment for invoice
        if paid_at:
            self.create_payment(invoice, XERO_ACCOUNT_STRIPE_PAYMENTS)

        # create fee payment for invoice
        # print("data", data)
        if data.get("fee", {}).get("fee"):
            fee = data["fee"]
            line_item = LineItem(
                line_item_id="", quantity=1, unit_amount=fee["fee"], line_amount=fee["fee"]
            )
            line_item.amount = fee["fee"]
            fee_invoice = self.create_invoice(
                acc_code=XERO_ACCOUNT_STRIPE_FEES,
                reference=f"Stripe fee {fee.get('id')}",
                contact=XERO_STRIPE_CONTACT_ID,
                description=f"Stripe fee for invoice {invoice.invoice_number}",
                total=fee["fee"] / 100,
                currency=data["currency"],
                date=invoice.date,
                lines=[line_item],
                invoice_no=f"Stripe fee {invoice.invoice_number}",
                paid_at=paid_at,
            )
            self.create_payment(fee_invoice, XERO_ACCOUNT_STRIPE_PAYMENTS)

        return invoice

    # util functions below

    def _tenant(self):
        if XERO_TENANT_ID:
            return XERO_TENANT_ID
        id_api = IdentityApi(self.client())
        result = id_api.get_connections()
        if len(result) > 1:
            raise Exception(f"More than one tenant found: {result}")
        return result[0].tenant_id

    def _convert_date(self, date):
        if not is_number(date):
            return date
        return datetime.fromtimestamp(date)

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
