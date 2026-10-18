"""Workflow checks use temporary JSON only; no gateway or email requests are made."""

import json
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from powerpick import create_app, database


class StoreWorkflows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-only", "JSON_FILE": str(Path(self.temp.name) / "store.json"),
            "BLOB_READ_WRITE_TOKEN": "", "DEMO_PAYMENTS": True, "RAZORPAY_KEY_ID": "", "RAZORPAY_KEY_SECRET": ""})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def post(self, path, data=None, client=None):
        client = client or self.client
        client.get("/")
        with client.session_transaction() as session:
            token = session["csrf_token"]
        return client.post(path, data={**(data or {}), "csrf_token": token})

    def register(self, client=None, email="electrician@example.com"):
        return self.post("/signup", {"name": "Test Electrician", "email": email, "password": "TestPassword123!", "terms": "on"}, client)

    def checkout(self, client=None, **overrides):
        client = client or self.client
        client.get("/checkout")
        with client.session_transaction() as session:
            key = session.get("checkout_key")
        payload = {"checkout_key": key, "name": "Test Electrician", "phone": "9876543210", "address": "12 Workshop Road, Near Market",
            "city": "Mumbai", "state": "Maharashtra", "pincode": "400001", "country": "India", "terms": "on", "payment_method": "demo"}
        return self.post("/checkout", {**payload, **overrides}, client)

    def snapshot(self):
        with self.app.app_context():
            return database.read()

    def order(self):
        self.register()
        self.post("/cart/add", {"product_id": 1, "quantity": 2, "price": 1})
        self.checkout()
        return self.snapshot()["orders"][0]

    def test_catalog_filters_and_generated_assets(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        result = self.client.get("/api/products?q=pliers").get_json()
        self.assertEqual(len(result["products"]), 1)
        self.assertEqual(result["currency"], "INR")
        self.assertTrue(all(p["stock"] > 0 for p in self.client.get("/api/products?stock=1").get_json()["products"]))
        for path in ["/assets/brand-icon.png", "/assets/products.png", "/css/styles.css", "/js/app.js"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            response.close()

    def test_csrf_role_and_password_protection(self):
        self.assertEqual(self.client.post("/cart/add", data={"product_id": 1}).status_code, 400)
        self.register()
        self.assertEqual(self.client.get("/admin").status_code, 403)
        user = self.snapshot()["users"][0]
        self.assertNotEqual(user["password_hash"], "TestPassword123!")
        self.assertNotIn("password", user)

    def test_checkout_reprices_and_demo_success_is_idempotent(self):
        order = self.order()
        self.assertEqual(order["total"], 64900 * 2 + 7900)
        self.assertEqual(order["tax"], 19800)
        self.assertEqual(order["payment_status"], "pending")
        reference = order["reference"]
        self.assertEqual(self.client.get(f"/orders/{reference}/payment").status_code, 200)
        self.post(f"/payments/demo/{reference}", {"outcome": "success"})
        self.post(f"/payments/demo/{reference}", {"outcome": "success"})
        updated = self.snapshot()["orders"][0]
        self.assertEqual(updated["payment_status"], "demo_paid")
        self.assertEqual(updated["status"], "placed")
        self.assertEqual(sum(e["title"] == "Demo payment approved" for e in updated["events"]), 1)
        self.assertEqual(self.client.get(f"/orders/{reference}").status_code, 200)
        self.assertEqual(self.client.get("/account").status_code, 200)

    def test_demo_failure_retry_and_cancel_release_stock_once(self):
        order = self.order()
        path = "/payments/demo/" + order["reference"]
        self.post(path, {"outcome": "failure"})
        self.assertEqual(self.snapshot()["orders"][0]["payment_status"], "demo_failed")
        self.post(path, {"outcome": "cancel"})
        self.post(path, {"outcome": "cancel"})
        self.assertEqual(self.snapshot()["products"][0]["stock"], 36)
        self.assertEqual(self.snapshot()["orders"][0]["status"], "cancelled")

    def test_india_only_address_enforcement(self):
        self.register()
        self.post("/cart/add", {"product_id": 1})
        self.checkout(country="USA", state="California", pincode="90210")
        self.assertEqual(len(self.snapshot()["orders"]), 0)
        self.checkout(payment_method="cod")
        self.assertEqual(self.snapshot()["orders"][0]["country"], "India")

    def test_other_customers_cannot_read_orders(self):
        order = self.order()
        other = self.app.test_client()
        self.register(other, "other@example.com")
        self.assertEqual(other.get("/orders/" + order["reference"]).status_code, 404)
        self.assertEqual(self.post("/payments/demo/" + order["reference"], {"outcome": "success"}, other).status_code, 404)

    def test_inventory_is_rechecked_when_two_baskets_compete(self):
        with self.app.app_context():
            database.mutate(lambda data: data["products"][0].update(stock=1))
        other = self.app.test_client()
        self.register()
        self.register(other, "other@example.com")
        for client in [self.client, other]:
            self.post("/cart/add", {"product_id": 1}, client)
        self.checkout(payment_method="cod")
        self.checkout(other, payment_method="cod")
        self.assertEqual(len(self.snapshot()["orders"]), 1)
        self.assertEqual(self.snapshot()["products"][0]["stock"], 0)

    def test_tracking_returns_reviews_and_customer_messages(self):
        order = self.order()
        reference = order["reference"]
        self.post("/payments/demo/" + reference, {"outcome": "success"})
        # A trusted setup operation promotes the test user, just like create-admin.
        with self.app.app_context():
            database.mutate(lambda data: data["users"][0].update(is_admin=True))
        for path in ["/admin", "/admin?tab=products", "/admin?tab=support", "/admin?tab=communications", "/admin/products/1/edit", "/admin/orders/" + reference]:
            self.assertEqual(self.client.get(path).status_code, 200)
        self.post("/admin/orders/" + reference, {"status": "packed"})
        self.post("/admin/orders/" + reference, {"status": "shipped", "tracking_carrier": "Test carrier", "tracking_number": "IN123", "tracking_url": "https://example.com/track/IN123"})
        self.post("/admin/orders/" + reference, {"status": "delivered"})
        self.assertEqual(self.client.get("/products/insulated-combination-pliers").status_code, 200)
        self.post("/products/1/reviews", {"rating": 5, "body": "Comfortable grip for everyday installation work."})
        self.assertEqual(len(self.snapshot()["reviews"]), 1)
        self.post("/orders/" + reference + "/return", {"reason": "The wrong size was ordered and I need a return."})
        self.post("/admin/orders/" + reference, {"status": "return_approved", "resolution": "Please follow the return instructions sent by support."})
        self.post("/admin/orders/" + reference, {"status": "returned"})
        data = self.snapshot()
        self.assertEqual(data["orders"][0]["payment_status"], "demo_refunded")
        self.assertEqual(data["products"][0]["stock"], 36)
        self.assertGreater(len(data["notifications"]), 3)

    def test_concurrent_json_mutations_do_not_lose_inventory_updates(self):
        def add_one(_):
            with self.app.app_context():
                database.mutate(lambda data: data["products"][0].update(stock=data["products"][0]["stock"] + 1))
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(add_one, range(12)))
        self.assertEqual(self.snapshot()["products"][0]["stock"], 48)
        self.assertEqual(json.loads(Path(self.app.config["JSON_FILE"]).read_text(encoding="utf-8"))["schema_version"], 1)

    def test_duplicate_checkout_key_does_not_create_a_second_order(self):
        order = self.order()
        self.post('/checkout', {"checkout_key": order["checkout_key"]})
        self.assertEqual(len(self.snapshot()["orders"]), 1)
        self.assertEqual(self.snapshot()["products"][0]["stock"], 34)

    def test_support_reply_and_admin_catalog_edit(self):
        self.register()
        self.post('/support', {"name": "Test Electrician", "email": "electrician@example.com", "subject": "Order question", "message": "Can you help with a delivery question?"})
        with self.app.app_context():
            database.mutate(lambda data: data['users'][0].update(is_admin=True))
        self.post('/admin/support/1/reply', {"reply": "Please share your order reference and we will check."})
        self.assertEqual(self.snapshot()['tickets'][0]['status'], 'answered')
        self.post('/admin/products/1/edit', {"name":"Updated pliers", "sku":"PP-TL-001", "category":"Tools & safety",
            "description":"A complete product description for the revised catalog item.", "specification":"200 mm", "unit":"each",
            "price":"599.50", "stock":"12", "tax_rate":"18", "image_index":"0", "image_url":"", "active":"on"})
        self.assertEqual(self.snapshot()['products'][0]['price'], 59950)
        self.assertEqual(self.snapshot()['products'][0]['stock'], 12)
        self.assertEqual(self.client.get('/admin/products/new').status_code, 200)

    def test_hosted_json_conflict_reloads_before_retrying(self):
        self.app.config['BLOB_READ_WRITE_TOKEN'] = 'test-token'
        first, newer = database.fresh_data(), database.fresh_data()
        newer['products'][0]['stock'] = 30
        with self.app.app_context(), patch('powerpick.database._blob_read', side_effect=[(first, 'old'), (newer, 'new')]), patch('powerpick.database._blob_write', side_effect=[database.DatabaseConflict('stale'), None]) as write:
            database.mutate(lambda data: data['products'][0].update(stock=data['products'][0]['stock'] - 1))
            self.assertEqual(write.call_args.args[0]['products'][0]['stock'], 29)
            self.assertEqual(write.call_args.args[1], 'new')


if __name__ == "__main__":
    unittest.main()
