# Stripe to Xero Sync

Small helper script to migrate customers and historical invoices from [Stripe](https://stripe.com)
into [Xero](https://xero.com) (or [QuickBooks](https://quickbooks.intuit.com)).

## Prerequisites

* `python`
* `pip`
* User account for Stripe and Xero

## Installation

To install the dependencies:
```
make install
```

## Configuration

The following options can be configured as environment variables:
* `STRIPE_SK`: Stripe secret key, required to fetch subscriptions from the Stripe API
* `XERO_STRIPE_CONTACT_ID`: ID of the Xero contact that represents the Stripe payment processor
* `XERO_TENANT_ID`: Xero tenant ID (unique ID of your organization)
* `XERO_CLIENT_ID`: Xero client ID (for API access)
* `XERO_CLIENT_SECRET`: Xero client secret (for API access)
* `XERO_ACCOUNT_STRIPE_SALES`: Xero account used to track Stripe sales (revenue)
* `XERO_ACCOUNT_STRIPE_FEES`: Xero account used to track the Stripe fees (bills/expenses)
* `XERO_ACCOUNT_STRIPE_PAYMENTS`: Xero account used to record Stripe payments (both subscriptions and fees)
* `START_DATE`/`END_DATE`: start and end date of invoices to be processed (e.g., `2021-01-01`-`2021-12-31`)

Additionally, you'll need to enable API access in Xero - make sure to configure an OAUth app in the
[Xero developer portal](https://developer.xero.com/app/manage), and set `https://localhost.localstack.cloud:54071/callback`
as the Redirect URI. The OAuth app configuration page will also contain the client ID, which you can
configure via the `XERO_CLIENT_ID` environment variable, see above.

## Usage

To perform a "dry run", i.e., only displaying the changes to be made, without actually applying them:
```
$ make dry-run
```

To run the migration script in live mode, i.e., effecting the changes in the Xero account:
```
$ make run
```
**Note: Be careful with running the script** - first double-check with the dry-run that all configurations
are correct, as the changes cannot easily be undone in your Xero account! It is highly recommended that you
first test this script in a staging/demo account, before actually running it against your production data.

Note that the script will ask you to obtain an API token by performing the auth flow in your browser:
```
Open this URL in your browser:
https://login.xero.com/identity/connect/authorize?response_type=code&client_id=C04...&redirect_uri=https://localhost.localstack.cloud:54071/callback&scope=openid+profile+email+accounting.transactions+accounting.settings+accounting.contacts+projects+assets
```
Once you have confirmed your username/password in the browser, the script will automatically receive the token via the callback endpoint, and will continue executing.

## License

This project is released under the Apache License, Version 2.0.
