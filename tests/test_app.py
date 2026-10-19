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

    def post(self, path, data=None):
        with self.client.session_transaction() as sess:
            sess["csrf_token"] = "test-csrf"
        return self.client.post(path, data={"csrf_token": "test-csrf", **(data or {})})

    def sign_in(self):
        self.query.return_value = [(42, self.password_hash)]
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
        for rows in ([], [(42, self.password_hash)]):
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
        self.query.side_effect = [[(42, "user@example.com", "Hasan")], [(1, "Burger Joint", "123 Main St")]]
        response = self.client.get("/")
        self.assertIn(b"Hi, Hasan", response.data)
        self.assertIn(b"Burger Joint", response.data)
        self.query.side_effect = None
        self.query.return_value = [(42, "user@example.com", "Hasan")]
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


if __name__ == "__main__":
    unittest.main()
