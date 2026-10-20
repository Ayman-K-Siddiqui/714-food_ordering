import os
import secrets
from datetime import timedelta
from functools import wraps
from collections import OrderedDict

import oracledb
from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
# Keep the development signing key across Flask reloads and container restarts.
# Deployments can supply their own key through SECRET_KEY.
secret_key = os.getenv("SECRET_KEY")
if not secret_key:
    os.makedirs(app.instance_path, exist_ok=True)
    key_path = os.path.join(app.instance_path, "session.key")
    try:
        with open(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as key_file:
            key_file.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(key_path) as key_file:
        secret_key = key_file.read().strip()
app.config.update(
    SECRET_KEY=secret_key,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)


def query_rows(sql, parameters=None):
    connection = oracledb.connect(
        user=ORACLE_USER, password=ORACLE_PASSWORD, dsn=ORACLE_DSN,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters or {})
            return cursor.fetchall()
    finally:
        connection.close()


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def load_current_user():
    g.user = None
    if request.method == "POST":
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected.encode(), supplied.encode()):
            abort(400, description="Your form expired. Refresh the page and try again.")
    user_id = session.get("user_id")
    if user_id is not None:
        try:
            rows = query_rows(
                "SELECT user_id, email, first_name FROM Users WHERE user_id = :user_id",
                {"user_id": user_id},
            )
        except oracledb.Error:
            return render_template("unavailable.html"), 503
        if rows:
            g.user = dict(zip(("user_id", "email", "first_name"), rows[0]))
        else:
            session.clear()


def login_required(view):
    """Use on cart/order routes so their owner comes from g.user['user_id']."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


ORACLE_USER = os.getenv("ORACLE_USER", "system")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "AdminPassword123")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")


def get_connection():
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )


def get_menu_items(restaurant_id):
    rows = query_rows(
        """
        SELECT item_id, name, description, price, category
        FROM MenuItems
        WHERE is_available = 1 AND restaurant_id = :restaurant_id
        ORDER BY category, name
        """,
        {"restaurant_id": restaurant_id},
    )
    return [dict(zip(("item_id", "name", "description", "price", "category"), row))
            for row in rows]


def group_by_category(items):
    grouped = OrderedDict()
    for item in items:
        grouped.setdefault(item["category"] or "Other", []).append(item)
    return grouped


def create_user(email, password, first_name, last_name):
    connection = oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO Users (email, password_hash, first_name, last_name)
                VALUES (:email, :password_hash, :first_name, :last_name)
                """,
                {
                    "email": email,
                    "password_hash": generate_password_hash(password),
                    "first_name": first_name or None,
                    "last_name": last_name or None,
                },
            )
        connection.commit()
    finally:
        connection.close()


def add_item_to_cart(user_id, item_id, quantity):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            sql = "INSERT INTO Cart (user_id, item_id, quantity) VALUES (:1, :2, :3)"
            try:
                cursor.execute(sql, [user_id, item_id, quantity])
            except oracledb.IntegrityError:
                cursor.execute(
                    """
                    UPDATE Cart
                    SET quantity = quantity + :1
                    WHERE user_id = :2 AND item_id = :3
                    """,
                    [quantity, user_id, item_id],
                )
        connection.commit()
    finally:
        connection.close()


@app.route("/")
def restaurants():
    error = None
    stores = []
    try:
        rows = query_rows("SELECT restaurant_id, name, location FROM Restaurants ORDER BY name")
        stores = [dict(zip(("restaurant_id", "name", "location"), row)) for row in rows]
    except oracledb.Error:
        error = "Could not load restaurants. Please try again shortly."
    return render_template("restaurants.html", restaurants=stores, error=error), 503 if error else 200


@app.route("/menu")
def menu():
    return redirect(url_for("restaurants"))


@app.route("/store/<int:restaurant_id>/menu")
@app.route("/restaurants/<int:restaurant_id>/menu")
def restaurant_menu(restaurant_id):
    try:
        rows = query_rows(
            "SELECT restaurant_id, name, location FROM Restaurants WHERE restaurant_id = :restaurant_id",
            {"restaurant_id": restaurant_id},
        )
        if not rows:
            abort(404)
        restaurant = dict(zip(("restaurant_id", "name", "location"), rows[0]))
        grouped_items = group_by_category(get_menu_items(restaurant_id))
    except oracledb.Error:
        return render_template("unavailable.html"), 503
    return render_template("menu.html", restaurant=restaurant, grouped_items=grouped_items,
                           cart_message=request.args.get("cart"))


@app.route("/add_to_cart", methods=["POST"])
@login_required
def add_to_cart():
    restaurant_id = request.form.get("restaurant_id", type=int)
    user_id = g.user["user_id"]
    item_id = request.form.get("item_id", type=int)
    quantity = request.form.get("quantity", default=1, type=int) or 1

    if not item_id or quantity < 1:
        return redirect(url_for("restaurants"))

    try:
        add_item_to_cart(user_id, item_id, quantity)
    except oracledb.Error:
        if restaurant_id:
            return redirect(
                url_for("restaurant_menu", restaurant_id=restaurant_id, cart="error")
            )
        return redirect(url_for("restaurants"))

    if restaurant_id:
        return redirect(url_for("restaurant_menu", restaurant_id=restaurant_id, cart="added"))
    return redirect(url_for("restaurants"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("restaurants"))
    error = None
    email = request.form.get("email", "").strip()
    status = 200
    if request.method == "POST":
        password = request.form.get("password", "")
        if not email or not password:
            error = "Email and password are required."
        else:
            try:
                rows = query_rows(
                    "SELECT user_id, password_hash FROM Users WHERE email = :email",
                    {"email": email},
                )
                if rows and check_password_hash(rows[0][1], password):
                    session.clear()
                    session["user_id"] = int(rows[0][0])
                    session.permanent = True
                    return redirect(url_for("restaurants"))
                error = "Invalid email or password."
            except oracledb.Error:
                error = "Login is temporarily unavailable. Please try again shortly."
                status = 503
    return render_template("login.html", error=error, email=email,
                           registered=request.args.get("registered") == "1"), status


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("restaurants"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("restaurants"))
    error = None
    form_data = {"email": "", "first_name": "", "last_name": ""}

    if request.method == "POST":
        form_data = {
            "email": request.form.get("email", "").strip(),
            "first_name": request.form.get("first_name", "").strip(),
            "last_name": request.form.get("last_name", "").strip(),
        }
        password = request.form.get("password", "")

        if not form_data["email"] or not password:
            error = "Email and password are required."
        else:
            try:
                create_user(password=password, **form_data)
                return redirect(url_for("login", registered="1"))
            except oracledb.IntegrityError:
                error = "An account with that email already exists."
            except oracledb.Error:
                error = "Could not create your account. Please try again."

    return render_template(
        "register.html",
        error=error,
        form_data=form_data,
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
