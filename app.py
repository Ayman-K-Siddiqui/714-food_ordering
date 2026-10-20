import os
from collections import OrderedDict

import oracledb
from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "cps714-dev-secret")

ORACLE_USER = os.getenv("ORACLE_USER", "system")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "AdminPassword123")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")


def get_connection():
    return oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )


def get_restaurants():
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT restaurant_id, name, location
                FROM Restaurants
                ORDER BY name
                """
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    return [
        {"restaurant_id": restaurant_id, "name": name, "location": location}
        for restaurant_id, name, location in rows
    ]


def get_restaurant(restaurant_id):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT restaurant_id, name, location
                FROM Restaurants
                WHERE restaurant_id = :restaurant_id
                """,
                {"restaurant_id": restaurant_id},
            )
            row = cursor.fetchone()
    finally:
        connection.close()

    if not row:
        return None
    return {"restaurant_id": row[0], "name": row[1], "location": row[2]}


def get_menu_items(restaurant_id):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item_id, name, description, price, category
                FROM MenuItems
                WHERE is_available = 1
                  AND restaurant_id = :restaurant_id
                ORDER BY category, name
                """,
                {"restaurant_id": restaurant_id},
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    return [
        {
            "item_id": item_id,
            "name": name,
            "description": description,
            "price": price,
            "category": category,
        }
        for item_id, name, description, price, category in rows
    ]


def group_by_category(items):
    grouped = OrderedDict()
    for item in items:
        grouped.setdefault(item["category"] or "Other", []).append(item)
    return grouped


def create_user(email, password, first_name, last_name):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            user_id_var = cursor.var(oracledb.DB_TYPE_NUMBER)
            cursor.execute(
                """
                INSERT INTO Users (email, password_hash, first_name, last_name)
                VALUES (:email, :password_hash, :first_name, :last_name)
                RETURNING user_id INTO :user_id
                """,
                {
                    "email": email,
                    "password_hash": generate_password_hash(password),
                    "first_name": first_name or None,
                    "last_name": last_name or None,
                    "user_id": user_id_var,
                },
            )
        connection.commit()
        return int(user_id_var.getvalue()[0])
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
def index():
    error = None
    restaurants = []
    try:
        restaurants = get_restaurants()
    except oracledb.Error as exc:
        error = str(exc)
    return render_template("restaurants.html", restaurants=restaurants, error=error)


@app.route("/menu")
def old_menu():
    return redirect(url_for("index"))


@app.route("/store/<int:restaurant_id>/menu")
def menu(restaurant_id):
    error = None
    grouped_items = OrderedDict()
    restaurant = None
    try:
        restaurant = get_restaurant(restaurant_id)
        if restaurant is None:
            error = "That store was not found."
        else:
            grouped_items = group_by_category(get_menu_items(restaurant_id))
    except oracledb.Error as exc:
        error = str(exc)
    return render_template(
        "menu.html",
        restaurant=restaurant,
        grouped_items=grouped_items,
        error=error,
        cart_message=request.args.get("cart"),
        logged_in=session.get("user_id") is not None,
    )


@app.route("/add_to_cart", methods=["POST"])
def add_to_cart():
    restaurant_id = request.form.get("restaurant_id", type=int)
    user_id = session.get("user_id")
    item_id = request.form.get("item_id", type=int)
    quantity = request.form.get("quantity", default=1, type=int) or 1

    if user_id is None:
        return redirect(url_for("register"))

    if not item_id:
        return redirect(url_for("index"))

    try:
        add_item_to_cart(user_id, item_id, quantity)
    except oracledb.Error:
        if restaurant_id:
            return redirect(
                url_for("menu", restaurant_id=restaurant_id, cart="error")
            )
        return redirect(url_for("index"))

    if restaurant_id:
        return redirect(url_for("menu", restaurant_id=restaurant_id, cart="added"))
    return redirect(url_for("index"))


@app.route("/register", methods=["GET", "POST"])
def register():
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
                session["user_id"] = create_user(password=password, **form_data)
                return redirect(url_for("register", registered="1"))
            except oracledb.IntegrityError:
                error = "An account with that email already exists."
            except oracledb.Error:
                error = "Could not create your account. Please try again."

    return render_template(
        "register.html",
        error=error,
        registered=request.args.get("registered") == "1",
        form_data=form_data,
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
