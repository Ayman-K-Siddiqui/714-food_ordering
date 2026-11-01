import os
import secrets
from datetime import timedelta
from functools import wraps
from collections import OrderedDict

import oracledb
from flask import Flask, abort, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

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


@app.context_processor
def inject_cart_count():
    """Number of items in the logged-in user's cart, for the nav badge."""
    count = 0
    if getattr(g, "user", None):
        try:
            connection = get_connection()
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT NVL(SUM(quantity), 0) FROM Cart WHERE user_id = :user_id",
                        {"user_id": g.user["user_id"]},
                    )
                    row = cursor.fetchone()
                    count = int(row[0]) if row else 0
            finally:
                connection.close()
        except oracledb.Error:
            count = 0
    return {"cart_count": count}


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


class DifferentRestaurantError(Exception):
    """Raised when a user adds an item from a different restaurant than the one already in their cart."""


def add_item_to_cart(user_id, item_id, quantity):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            # Which restaurant is this item from?
            cursor.execute(
                "SELECT restaurant_id FROM MenuItems WHERE item_id = :item_id",
                {"item_id": item_id},
            )
            item = cursor.fetchone()
            if item is None:
                raise ValueError("Item not found")

            # Which restaurant is already in the cart (if any)?
            cursor.execute(
                """
                SELECT m.restaurant_id
                FROM Cart c
                JOIN MenuItems m ON m.item_id = c.item_id
                WHERE c.user_id = :user_id
                FETCH FIRST 1 ROWS ONLY
                """,
                {"user_id": user_id},
            )
            existing = cursor.fetchone()
            if existing and existing[0] != item[0]:
                raise DifferentRestaurantError()

            try:
                cursor.execute(
                    "INSERT INTO Cart (user_id, item_id, quantity) VALUES (:1, :2, :3)",
                    [user_id, item_id, quantity],
                )
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


def place_order(user_id):
    """Turn the user's cart into an order. Returns the new order_id, or None if the cart is empty."""
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            # Read the cart with prices. FOR UPDATE locks the rows so a double-click can't order twice.
            cursor.execute(
                """
                SELECT c.item_id, c.quantity, m.price, m.restaurant_id
                FROM Cart c
                JOIN MenuItems m ON m.item_id = c.item_id
                WHERE c.user_id = :user_id
                FOR UPDATE OF c.quantity
                """,
                {"user_id": user_id},
            )
            rows = cursor.fetchall()
            if not rows:
                return None

            if len({row[3] for row in rows}) > 1:
                raise ValueError("Cart has items from more than one restaurant")
            restaurant_id = rows[0][3]
            total = sum(quantity * price for _, quantity, price, _ in rows)

            # Create the order and get its new ID back
            new_id = cursor.var(oracledb.NUMBER)
            cursor.execute(
                """
                INSERT INTO Orders (user_id, restaurant_id, total, status)
                VALUES (:user_id, :restaurant_id, :total, 'Pending')
                RETURNING order_id INTO :new_id
                """,
                {"user_id": user_id, "restaurant_id": restaurant_id,
                 "total": total, "new_id": new_id},
            )
            order_id = int(new_id.getvalue()[0])

            # Copy every cart item into OrderItems, keeping the price paid
            cursor.executemany(
                """
                INSERT INTO OrderItems (order_id, item_id, quantity, price_at_order)
                VALUES (:1, :2, :3, :4)
                """,
                [(order_id, item_id, quantity, price) for item_id, quantity, price, _ in rows],
            )

            cursor.execute("DELETE FROM Cart WHERE user_id = :user_id", {"user_id": user_id})

        connection.commit()  # everything saves together, or nothing does
        return order_id
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


ORDER_STATUSES = ["Pending", "Preparing", "Ready", "Completed"]


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
    except DifferentRestaurantError:
        if restaurant_id:
            return redirect(
                url_for("restaurant_menu", restaurant_id=restaurant_id, cart="other_restaurant")
            )
        return redirect(url_for("restaurants"))
    except (oracledb.Error, ValueError):
        if restaurant_id:
            return redirect(
                url_for("restaurant_menu", restaurant_id=restaurant_id, cart="error")
            )
        return redirect(url_for("restaurants"))

    if restaurant_id:
        return redirect(url_for("restaurant_menu", restaurant_id=restaurant_id, cart="added"))
    return redirect(url_for("restaurants"))


@app.route("/checkout", methods=["POST"])
@login_required
def checkout():
    try:
        order_id = place_order(g.user["user_id"])
    except (oracledb.Error, ValueError):
        return redirect(url_for("cart", checkout="error"))
    if order_id is None:
        return redirect(url_for("cart", checkout="empty"))
    return redirect(url_for("order_details", order_id=order_id, placed=1))


@app.route("/kitchen")
@login_required
def kitchen():
    view = request.args.get("view", "active")
    if view not in ("active", "completed"):
        view = "active"
    restaurant_id = request.args.get("restaurant_id", type=int)

    status_sql = "o.status <> 'Completed'" if view == "active" else "o.status = 'Completed'"
    params = {}
    restaurant_sql = ""
    if restaurant_id:
        restaurant_sql = " AND o.restaurant_id = :restaurant_id"
        params["restaurant_id"] = restaurant_id

    try:
        restaurant_rows = query_rows("SELECT restaurant_id, name FROM Restaurants ORDER BY name")
        order_rows = query_rows(
            f"""
            SELECT o.order_id, o.created_at, o.status, o.total, r.name, u.email
            FROM Orders o
            JOIN Restaurants r ON r.restaurant_id = o.restaurant_id
            JOIN Users u ON u.user_id = o.user_id
            WHERE {status_sql}{restaurant_sql}
            ORDER BY o.created_at {"ASC" if view == "active" else "DESC"}
            """,
            params,
        )
        item_rows = query_rows(
            f"""
            SELECT oi.order_id, m.name, oi.quantity
            FROM OrderItems oi
            JOIN MenuItems m ON m.item_id = oi.item_id
            JOIN Orders o ON o.order_id = oi.order_id
            WHERE {status_sql}{restaurant_sql}
            ORDER BY oi.order_item_id
            """,
            params,
        )
    except oracledb.Error:
        return render_template("unavailable.html"), 503

    items_by_order = {}
    for order_id, name, quantity in item_rows:
        items_by_order.setdefault(order_id, []).append({"name": name, "quantity": quantity})

    orders = [
        {"order_id": oid, "created_at": created, "status": status, "total": total,
         "restaurant": rname, "customer": email, "lines": items_by_order.get(oid, [])}
        for oid, created, status, total, rname, email in order_rows
    ]
    restaurants_list = [{"restaurant_id": rid, "name": name} for rid, name in restaurant_rows]
    return render_template(
        "kitchen.html", orders=orders, statuses=ORDER_STATUSES, view=view,
        restaurants=restaurants_list, selected_restaurant=restaurant_id,
    )


@app.route("/kitchen/<int:order_id>/status", methods=["POST"])
@login_required
def update_order_status(order_id):
    status = request.form.get("status")
    if status not in ORDER_STATUSES:
        abort(400)
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE Orders SET status = :status WHERE order_id = :order_id",
                {"status": status, "order_id": order_id},
            )
        connection.commit()
    except oracledb.Error:
        return render_template("unavailable.html"), 503
    finally:
        connection.close()
    return redirect(url_for(
        "kitchen",
        view=request.form.get("view", "active"),
        restaurant_id=request.form.get("restaurant_id", type=int) or None,
    ))


def get_cart(user_id):
    rows = query_rows(
        """
        SELECT c.item_id, m.name, m.price, c.quantity, r.restaurant_id, r.name
        FROM Cart c
        JOIN MenuItems m ON m.item_id = c.item_id
        JOIN Restaurants r ON r.restaurant_id = m.restaurant_id
        WHERE c.user_id = :user_id
        ORDER BY m.name
        """,
        {"user_id": user_id},
    )
    items = [
        {"item_id": item_id, "name": name, "price": price, "quantity": quantity,
         "line_total": price * quantity, "restaurant_id": rid, "restaurant": rname}
        for item_id, name, price, quantity, rid, rname in rows
    ]
    return items, sum(item["line_total"] for item in items)


def run_write(sql, parameters):
    connection = get_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


@app.route("/cart")
@login_required
def cart():
    try:
        items, total = get_cart(g.user["user_id"])
    except oracledb.Error:
        return render_template("unavailable.html"), 503
    return render_template(
        "cart.html", items=items, total=total,
        checkout_message=request.args.get("checkout"),
        cart_message=request.args.get("cart"),
    )


@app.route("/cart/update", methods=["POST"])
@login_required
def update_cart():
    item_id = request.form.get("item_id", type=int)
    quantity = request.form.get("quantity", type=int)
    if not item_id or quantity is None or quantity < 0:
        return redirect(url_for("cart", cart="error"))
    try:
        if quantity == 0:
            run_write(
                "DELETE FROM Cart WHERE user_id = :user_id AND item_id = :item_id",
                {"user_id": g.user["user_id"], "item_id": item_id},
            )
        else:
            run_write(
                "UPDATE Cart SET quantity = :quantity WHERE user_id = :user_id AND item_id = :item_id",
                {"quantity": min(quantity, 99), "user_id": g.user["user_id"], "item_id": item_id},
            )
    except oracledb.Error:
        return redirect(url_for("cart", cart="error"))
    return redirect(url_for("cart", cart="updated"))


@app.route("/cart/remove", methods=["POST"])
@login_required
def remove_from_cart():
    item_id = request.form.get("item_id", type=int)
    if not item_id:
        return redirect(url_for("cart", cart="error"))
    try:
        run_write(
            "DELETE FROM Cart WHERE user_id = :user_id AND item_id = :item_id",
            {"user_id": g.user["user_id"], "item_id": item_id},
        )
    except oracledb.Error:
        return redirect(url_for("cart", cart="error"))
    return redirect(url_for("cart", cart="removed"))


@app.route("/orders")
@login_required
def orders():
    try:
        rows = query_rows(
            """
            SELECT o.order_id, o.created_at, r.name, o.total, o.status,
                   (SELECT NVL(SUM(oi.quantity), 0) FROM OrderItems oi WHERE oi.order_id = o.order_id)
            FROM Orders o
            JOIN Restaurants r ON r.restaurant_id = o.restaurant_id
            WHERE o.user_id = :user_id
            ORDER BY o.created_at DESC
            """,
            {"user_id": g.user["user_id"]},
        )
    except oracledb.Error:
        return render_template("unavailable.html"), 503
    order_list = [
        {"order_id": oid, "created_at": created, "restaurant": rname,
         "total": total, "status": status, "item_count": int(count)}
        for oid, created, rname, total, status, count in rows
    ]
    return render_template("orders.html", orders=order_list)


@app.route("/orders/<int:order_id>")
@login_required
def order_details(order_id):
    try:
        rows = query_rows(
            """
            SELECT o.order_id, o.created_at, r.name, r.location, o.total, o.status
            FROM Orders o
            JOIN Restaurants r ON r.restaurant_id = o.restaurant_id
            WHERE o.order_id = :order_id AND o.user_id = :user_id
            """,
            {"order_id": order_id, "user_id": g.user["user_id"]},
        )
        if not rows:
            abort(404)  # not found, or someone else's order
        item_rows = query_rows(
            """
            SELECT m.name, oi.quantity, oi.price_at_order
            FROM OrderItems oi
            JOIN MenuItems m ON m.item_id = oi.item_id
            WHERE oi.order_id = :order_id
            ORDER BY oi.order_item_id
            """,
            {"order_id": order_id},
        )
    except oracledb.Error:
        return render_template("unavailable.html"), 503
    oid, created, rname, location, total, status = rows[0]
    order = {"order_id": oid, "created_at": created, "restaurant": rname,
             "location": location, "total": total, "status": status}
    lines = [
        {"name": name, "quantity": quantity, "price": price, "line_total": price * quantity}
        for name, quantity, price in item_rows
    ]
    return render_template(
        "order_details.html", order=order, lines=lines,
        statuses=ORDER_STATUSES, placed=request.args.get("placed") == "1",
    )


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
