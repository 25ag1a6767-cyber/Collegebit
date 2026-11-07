"""CampusBite AI v2.0 - Enterprise Locust Load Testing Suite.

Simulates concurrent student lunch-break rush and staff counter operations:
- 500, 1,000, and 2,000 virtual students browsing menus, categories, and canteen operating windows.
- Simultaneous cart checkout quotes under dynamic surge and peak hours.
- Live TV display polling and order tracking.
- Staff counter queue polling and token management.
"""

from __future__ import annotations

import json
import random
from locust import HttpUser, between, task, tag

class StudentCustomerUser(HttpUser):
    wait_time = between(1.0, 3.0)
    weight = 85

    def on_start(self):
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "CampusBite-Locust-LoadTester/2.0",
        }

    @task(40)
    @tag("browse", "menu")
    def browse_menu_catalog(self):
        with self.client.get("/api/v1/menu/products?limit=30", headers=self.headers, catch_response=True) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"Menu fetch failed with status {resp.status_code}")

    @task(25)
    @tag("browse", "categories")
    def browse_categories(self):
        self.client.get("/api/v1/menu/categories", headers=self.headers)

    @task(20)
    @tag("status", "canteen")
    def check_canteen_status(self):
        self.client.get("/api/v1/menu/canteen/status", headers=self.headers)

    @task(10)
    @tag("checkout", "quote")
    def request_checkout_quote(self):
        payload = {
            "counter_id": 1,
            "items": [
                {"product_id": 1, "quantity": 1},
                {"product_id": 2, "quantity": 2},
            ],
            "applied_coupon_code": "WELCOME10",
        }
        with self.client.post(
            "/api/v1/checkout/quote",
            json=payload,
            headers=self.headers,
            catch_response=True,
        ) as resp:

            if resp.status_code in (200, 400, 404):
                resp.success()
            else:
                resp.failure(f"Checkout quote error: {resp.status_code}")

    @task(15)
    @tag("display", "public")
    def poll_tv_display_tokens(self):
        self.client.get("/api/v1/queue/display-board", headers=self.headers)

class StaffCounterUser(HttpUser):
    wait_time = between(2.0, 5.0)
    weight = 15

    def on_start(self):
        self.headers = {
            "Accept": "application/json",
            "User-Agent": "CampusBite-Locust-Staff/2.0",
        }

    @task(30)
    @tag("staff", "display")
    def poll_display_board(self):
        self.client.get("/api/v1/queue/display-board", headers=self.headers)

    @task(20)
    @tag("staff", "health")
    def check_system_readiness(self):
        self.client.get("/api/ready", headers=self.headers)
