#!/usr/bin/env python3

from imgurpython import ImgurClient

import config

CONFIG_FILE = "config.yaml"


def authenticate():
    conf = config.read_file(CONFIG_FILE)
    # Get client ID and secret from auth.ini

    client = ImgurClient(conf.imgur.client_id, conf.imgur.client_secret)

    # Authorization flow, pin example (see docs for other auth types)
    authorization_url = client.get_auth_url('pin')

    print("Go to the following URL: {0}".format(authorization_url))

    # Read in the pin
    pin = input("Enter pin code: ")

    # ... redirect user to `authorization_url`, obtain pin (or code or token) ...
    credentials = client.authorize(pin, 'pin')
    client.set_user_auth(credentials['access_token'], credentials['refresh_token'])

    print("Authentication successful! Here are the details:")
    print("   Access token:  {0}".format(credentials['access_token']))
    print("   Refresh token: {0}".format(credentials['refresh_token']))

if __name__ == "__main__":
    authenticate()
