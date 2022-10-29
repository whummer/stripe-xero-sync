# Note: old migration code for QuickBooks - deprecated, currently not being used!

import json
import logging
import os
from urllib.parse import parse_qs

from stripe_xero.utils import REDIRECT_URL, BaseClient

LOG = logging.getLogger(__name__)

# QuickBooks configs
QB_COMPANY_ID = os.environ["QB_COMPANY_ID"]

CACHE = {}


class QuickBooksClient(BaseClient):
    """Client for QuickBooks. Deprecated - use XeroClient instead!"""

    def __init__(self):
        self._qb_client = None
        raise Exception("deprecated")

    def customers(self):
        from quickbooks.objects.customer import Customer

        return Customer.all(qb=self.client())

    def create_customer(self, data):
        from quickbooks.objects import Address as QBAddress
        from quickbooks.objects import EmailAddress as QBEmailAddress
        from quickbooks.objects import PhoneNumber as QBPhoneNumber
        from quickbooks.objects.customer import Customer

        existing = self.get_customer(data)
        if existing:
            return existing
        client = self.client()

        customer = Customer()
        customer.CompanyName = data["name"]
        customer.DisplayName = data["name"]
        customer.PrimaryEmailAddr = QBEmailAddress()
        customer.PrimaryEmailAddr.Address = data.get("email")
        customer.PrimaryPhone = QBPhoneNumber()
        customer.PrimaryPhone.FreeFormNumber = data.get("phone")
        if data.get("id"):
            customer.Notes = json.dumps({"stripe_customer_id": data["id"]})

        address = data.get("address") or {}
        customer.BillAddr = QBAddress()
        customer.BillAddr.Line1 = address.get("line1")
        customer.BillAddr.Line2 = address.get("line2")
        customer.BillAddr.City = address.get("city")
        customer.BillAddr.PostalCode = address.get("postal_code")
        customer.BillAddr.Country = address.get("country")
        customer.BillAddr.CountrySubDivisionCode = address.get("state")

        customer.save(qb=client)
        CACHE.pop("customers")
        return customer

    def get_customer(self, customer):
        customers = CACHE.get("customers")
        if not customers:
            CACHE["customers"] = customers = self.customers()

        def _matches(c):
            return expected in c.Notes or ""

        expected = json.dumps({"stripe_customer_id": customer["id"]})
        existing = [c for c in customers if _matches(c)]
        return (existing or [None])[0]

    def get_or_create_customer(self, customer):
        LOG.debug("Creating customer:", customer["name"])
        existing = self.get_customer(customer)
        if existing:
            LOG.debug(f"Customer {customer['name']} already exists, skipping ...")
            return existing
        return self.create_customer(customer)

    def _get_client(self, result_queue):
        from intuitlib.enums import Scopes
        from quickbooks import QuickBooks

        auth_client = self._auth_client()
        url = auth_client.get_authorization_url([Scopes.ACCOUNTING])
        print(f"Open this URL in your browser:\n{url}")

        # retrieve auth result asynchronously from callback
        result = result_queue.get()
        result = parse_qs(result.partition("?")[2])
        code = result.get("code")[0]
        realm_id = result.get("realmId")[0]

        # finalize auth flow
        auth_client.get_bearer_token(code, realm_id=realm_id)

        # return client
        return QuickBooks(auth_client=auth_client, company_id=QB_COMPANY_ID)

    def _auth_client(self):
        from intuitlib.client import AuthClient

        # TODO!
        settings = {}

        client_id = settings.get("qb.client_id")
        client_secret = settings.get("qb.client_secret")
        auth_client = AuthClient(
            client_id=client_id,
            client_secret=client_secret,
            environment="production",
            redirect_uri=REDIRECT_URL,
        )
        return auth_client
