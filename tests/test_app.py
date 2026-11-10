import unittest
from decimal import Decimal
from unittest.mock import patch

import oracledb
from werkzeug.security import generate_password_hash

import app as application


class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("test-password")

    def setUp(self):
        application.app.config.update(TESTING=True, SECRET_KEY="test-key")
        self.client = application.app.test_client()
        self.query = patch("app.query_rows").start()
        self.addCleanup(patch.stopall)
        self.query.return_value = []
        self.connection = patch("app.get_connection").start()
        self.connection.return_value.cursor.return_value.__enter__.return_value.fetchone.return_value = (0,)

    def post(self, path, data=None):
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf"
        return self.client.post(path, data={"csrf_token": "test-csrf", **(data or {})})

    def sign_in(self):
        self.query.return_value = [(42, self.password_hash, "customer")]
        return self.post("/login", {"email": "user@example.com", "password": "test-password"})

    def test_login_uses_existing_password_hash_and_stores_only_identity(self):
        response = self.sign_in()
        self.assertEqual(response.location, "/")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["user_id"], 42)
            self.assertNotIn("password", sess)
            self.assertNotIn("password_hash", sess)
        self.assertIn("HttpOnly", response.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", response.headers["Set-Cookie"])

    def test_invalid_credentials_do_not_authenticate(self):
        for rows in ([], [(42, self.password_hash, "customer")]):
            with self.subTest(rows=bool(rows)):
                self.query.return_value = rows
                response = self.post("/login", {"email": "user@example.com", "password": "wrong"})
                self.assertIn(b"Invalid email or password", response.data)
                with self.client.session_transaction() as sess:
                    self.assertNotIn("user_id", sess)

    def test_login_requires_fields(self):
        self.assertIn(b"Email and password are required", self.post("/login").data)
        self.query.assert_not_called()

    def test_session_survives_navigation_and_logout_clears_it(self):
        self.sign_in()
        self.query.side_effect = [[(42, "user@example.com", "Hasan", "customer", None)], [(1, "Burger Joint", "123 Main St")]]
        response = self.client.get("/")
        self.assertIn(b"Hi, Hasan", response.data)
        self.assertIn(b"Burger Joint", response.data)
        self.query.side_effect = None
        self.query.return_value = [(42, "user@example.com", "Hasan", "customer", None)]
        self.assertEqual(self.post("/logout").status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)
        self.assertEqual(self.client.get("/logout").status_code, 405)

    def test_deleted_user_session_is_cleared(self):
        self.sign_in()
        self.query.return_value = []
        response = self.client.get("/")
        self.assertIn(b"Log in", response.data)
        with self.client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_csrf_required_for_all_account_forms(self):
        for path in ("/login", "/register", "/logout"):
            self.assertEqual(self.client.post(path).status_code, 400)

    def test_csrf_unicode_input_is_rejected(self):
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf"
        response = self.client.post("/login", data={"csrf_token": "é"})
        self.assertEqual(response.status_code, 400)

    def test_separate_clients_do_not_share_identity(self):
        self.sign_in()
        other_client = application.app.test_client()
        self.query.return_value = []
        response = other_client.get("/")
        self.assertIn(b"Log in", response.data)
        with other_client.session_transaction() as sess:
            self.assertNotIn("user_id", sess)

    def test_login_required_uses_current_user(self):
        from flask import g

        @application.login_required
        def protected():
            return str(g.user["user_id"])

        with application.app.test_request_context("/"):
            g.user = None
            self.assertEqual(protected().location, "/login")
            g.user = {"user_id": 42}
            self.assertEqual(protected(), "42")

    def test_registration_redirects_to_login(self):
        with patch("app.create_user") as create:
            response = self.post("/register", {"email": "user@example.com", "password": "test-password"})
        self.assertEqual(response.location, "/login?registered=1")
        create.assert_called_once()

    def test_restaurants_and_menu_links(self):
        self.query.return_value = [(1, "Burger Joint", "123 Main St"), (2, "Pizza Palace", "456 Queen St")]
        response = self.client.get("/")
        self.assertIn(b"Burger Joint", response.data)
        self.assertIn(b"Pizza Palace", response.data)
        self.assertIn(b'/restaurants/2/menu', response.data)

    def test_selected_menu_filters_by_restaurant(self):
        self.query.side_effect = [[(2, "Pizza Palace", "456 Queen St")], [(7, "Margherita Pizza", "Basil", Decimal("12.99"), "Pizza")]]
        response = self.client.get("/restaurants/2/menu")
        self.assertIn(b"Margherita Pizza", response.data)
        self.assertNotIn(b"Cheeseburger", response.data)
        self.assertIn("restaurant_id = :restaurant_id", self.query.call_args.args[0])
        self.assertEqual(self.query.call_args.args[1], {"restaurant_id": 2})

    def test_unknown_restaurant_is_404(self):
        self.assertEqual(self.client.get("/restaurants/999/menu").status_code, 404)

    def test_empty_restaurants_and_menu(self):
        self.assertIn(b"No restaurants are available", self.client.get("/").data)
        self.query.side_effect = [[(1, "Burger Joint", "123 Main St")], []]
        self.assertIn(b"No menu items are available", self.client.get("/restaurants/1/menu").data)

    def test_database_failure_is_generic_503(self):
        self.query.side_effect = oracledb.DatabaseError("private database details")
        for path in ("/", "/restaurants/1/menu"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(b"private database details", response.data)
        response = self.post("/login", {"email": "user@example.com", "password": "test-password"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private database details", response.data)

    def test_legacy_menu_redirects_to_selection(self):
        self.assertEqual(self.client.get("/menu").location, "/")

    def test_cart_requires_login_and_csrf(self):
        with patch("app.add_item_to_cart") as add:
            self.assertEqual(self.client.post("/add_to_cart").status_code, 400)
            self.assertEqual(self.post("/add_to_cart").location, "/login")
        add.assert_not_called()

    def test_cart_uses_authenticated_user_and_returns_to_menu(self):
        self.sign_in()
        self.query.return_value = [(42, "user@example.com", "Hasan", "customer", None)]
        with patch("app.add_item_to_cart") as add:
            response = self.post("/add_to_cart", {
                "user_id": "999", "item_id": "7", "quantity": "2", "restaurant_id": "2",
            })
        add.assert_called_once_with(42, 7, 2)
        self.assertEqual(response.location, "/restaurants/2/menu?cart=added")

    def test_cart_database_failure_returns_to_menu(self):
        self.sign_in()
        self.query.return_value = [(42, "user@example.com", "Hasan", "customer", None)]
        with patch("app.add_item_to_cart", side_effect=oracledb.DatabaseError("private details")):
            response = self.post("/add_to_cart", {"item_id": "7", "restaurant_id": "2"})
        self.assertEqual(response.location, "/restaurants/2/menu?cart=error")

    def test_store_menu_alias_renders_cart_form(self):
        self.query.side_effect = [
            [(2, "Pizza Palace", "456 Queen St")],
            [(7, "Margherita Pizza", "Basil", Decimal("12.99"), "Pizza")],
        ]
        response = self.client.get("/store/2/menu")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'action="/add_to_cart"', response.data)
        self.assertIn(b'name="csrf_token"', response.data)

    def authenticate(self, role="staff", restaurant_id=2):
        with self.client.session_transaction() as sess:
            sess["user_id"] = 42
        self.query.return_value = [(42, "staff@example.com", "Staff", role, restaurant_id)]

    def test_staff_login_redirects_to_kitchen(self):
        self.query.return_value = [(42, self.password_hash, "staff")]
        response = self.post("/login", {"email": "staff@example.com", "password": "test-password"})
        self.assertEqual(response.location, "/kitchen")

    def test_signed_in_account_pages_redirect_by_role(self):
        for role, destination in (("customer", "/"), ("staff", "/kitchen")):
            self.authenticate(role)
            for path in ("/login", "/register"):
                self.assertEqual(self.client.get(path).location, destination)

    def test_kitchen_requires_staff_for_read_and_write(self):
        self.assertEqual(self.client.get("/kitchen").location, "/login")
        self.assertEqual(self.post("/kitchen/1/status", {"status": "Ready"}).location, "/login")
        for role, restaurant_id in (("customer", None), ("staff", None)):
            self.authenticate(role, restaurant_id)
            self.assertEqual(self.client.get("/kitchen").status_code, 403)
            self.assertEqual(self.post("/kitchen/1/status", {"status": "Ready"}).status_code, 403)
        self.connection.assert_not_called()

    def test_kitchen_scopes_orders_and_items_to_staff_restaurant(self):
        from datetime import datetime
        for view in ("active", "completed"):
            self.authenticate()
            self.query.side_effect = [
                [(42, "staff@example.com", "Staff", "staff", 2)],
                [(7, datetime(2026, 11, 9), "Ready", Decimal("12.99"), "Pizza Palace", "customer@example.com")],
                [(7, "Pizza", 2)],
            ]
            self.query.reset_mock()
            response = self.client.get(f"/kitchen?restaurant_id=1&view={view}")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Pizza Palace", response.data)
            self.assertIn(b"Pizza", response.data)
            self.assertNotIn(b"All restaurants", response.data)
            for call in self.query.call_args_list[1:]:
                self.assertIn("o.restaurant_id = :restaurant_id", call.args[0])
                self.assertEqual(call.args[1], {"restaurant_id": 2})
            self.query.side_effect = None

    def test_status_update_cannot_target_another_restaurant(self):
        self.authenticate()
        cursor = self.connection.return_value.cursor.return_value.__enter__.return_value
        for affected, expected in ((0, 404), (1, 302)):
            cursor.rowcount = affected
            self.connection.return_value.commit.reset_mock()
            response = self.post("/kitchen/7/status", {
                "status": "Ready", "restaurant_id": "1", "view": "completed",
            })
            self.assertEqual(response.status_code, expected)
            self.assertIn("restaurant_id = :restaurant_id", cursor.execute.call_args.args[0])
            self.assertEqual(cursor.execute.call_args.args[1], {
                "status": "Ready", "order_id": 7, "restaurant_id": 2,
            })
            self.assertEqual(self.connection.return_value.commit.called, bool(affected))
            if affected:
                self.assertEqual(response.location, "/kitchen?view=completed")

    def test_staff_status_update_requires_valid_status_and_csrf(self):
        self.authenticate()
        self.assertEqual(self.client.post("/kitchen/7/status").status_code, 400)
        self.assertEqual(self.post("/kitchen/7/status", {"status": "invalid"}).status_code, 400)
        self.connection.assert_not_called()

    def test_navigation_by_role(self):
        for role in ("staff", "customer"):
            with application.app.test_request_context():
                from flask import g, render_template
                g.user = {"user_id": 42, "role": role, "first_name": "Test", "email": "test@example.com"}
                html = render_template("base.html", cart_count=0)
            self.assertEqual('href="/kitchen"' in html, role == "staff")
            self.assertEqual('>Restaurants</a>' in html, role == "customer")
            self.assertEqual('href="/cart"' in html, role == "customer")
            self.assertEqual('>My Orders</a>' in html, role == "customer")


if __name__ == "__main__":
    unittest.main()
