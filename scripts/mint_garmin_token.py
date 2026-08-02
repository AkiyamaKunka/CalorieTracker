#!/usr/bin/env python3
"""Mint a Garmin Connect session token for the server's daily-activity pull.

RUN THIS YOURSELF, IN YOUR OWN TERMINAL — it asks for your Garmin
credentials interactively (password via getpass, never echoed, never
stored). What it saves is the resulting SESSION TOKEN directory, which the
server resumes without ever seeing your password (garmin.py's design).

Usage:
    python3 scripts/mint_garmin_token.py            # international garmin.com
    python3 scripts/mint_garmin_token.py --cn       # 中国大陆账号 garmin.cn

On success the tokens land in ~/.garminconnect_cn (or ~/.garminconnect),
ready to copy to the VM:  GARMIN_TOKEN_DIR=<that path>, GARMIN_ENABLED=1,
and for --cn also GARMIN_IS_CN=1.
"""
import getpass
import sys
from pathlib import Path

try:
    from garminconnect import Garmin
except ImportError:
    sys.exit("garminconnect is not installed — run: python3 -m pip install garminconnect")


def main() -> None:
    is_cn = "--cn" in sys.argv
    domain = "garmin.cn (中国大陆)" if is_cn else "garmin.com (international)"
    token_dir = Path.home() / (".garminconnect_cn" if is_cn else ".garminconnect")

    print(f"Minting a Garmin session token for {domain}")
    print("Your password is used ONCE, here, and never stored.\n")
    email = input("Garmin account email/phone: ").strip()
    password = getpass.getpass("Password (hidden): ")

    client = Garmin(email=email, password=password, is_cn=is_cn)
    result = client.login()
    # Newer releases return (oauth1, oauth2) or need an MFA round-trip;
    # handle the documented MFA shape when it appears.
    if isinstance(result, tuple) and result and result[0] == "needs_mfa":
        code = input("MFA code (from SMS/email/app): ").strip()
        client.resume_login(result[1], code)

    client.garth.dump(str(token_dir))
    print(f"\n✅ Token saved to {token_dir}")
    print("It refreshes itself; your password was not written anywhere.")
    print("Tell Claude it's ready — the copy to the VM happens file-to-file.")


if __name__ == "__main__":
    main()
