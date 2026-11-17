#!/usr/bin/env python3
"""Generate $0 ECDSA P-256 Web Push VAPID keys for .env configuration."""
from __future__ import annotations

import base64
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def main() -> None:
    vapid = Vapid01()
    vapid.generate_keys()
    raw_pub = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    raw_priv = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
    pub_b64 = base64.urlsafe_b64encode(raw_pub).rstrip(b"=").decode("ascii")
    priv_b64 = base64.urlsafe_b64encode(raw_priv).rstrip(b"=").decode("ascii")
    print("# Paste these into your .env file for 100% free Web Push (Android Chrome + iPhone PWA):")
    print(f"VAPID_PUBLIC_KEY={pub_b64}")
    print(f"VAPID_PRIVATE_KEY={priv_b64}")
    print("VAPID_SUBJECT=mailto:canteen@example.edu")


if __name__ == "__main__":
    main()
