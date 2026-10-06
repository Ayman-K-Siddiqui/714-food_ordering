import os
from collections import OrderedDict

import oracledb
from flask import Flask, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

app = Flask(__name__)

ORACLE_USER = os.getenv("ORACLE_USER", "system")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "AdminPassword123")
ORACLE_DSN = os.getenv("ORACLE_DSN", "localhost:1521/FREEPDB1")


def get_menu_items():
    connection = oracledb.connect(
        user=ORACLE_USER,
        password=ORACLE_PASSWORD,
        dsn=ORACLE_DSN,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT item_id, name, description, price, category
                FROM MenuItems
                WHERE is_available = 1
                ORDER BY category, name
                """
            )
            rows = cursor.fetchall()
    finally:
        connection.close()

    items = [
        {
            "item_id": item_id,
            "name": name,
            "description": description,
            "price": price,
            "category": category,
        }
        for item_id, name, description, price, category in rows
    ]
    return items


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


@app.route("/")
@app.route("/menu")
def menu():
    error = None
    grouped_items = OrderedDict()
    try:
        grouped_items = group_by_category(get_menu_items())
    except oracledb.Error as exc:
        error = str(exc)
    return render_template("menu.html", grouped_items=grouped_items, error=error)


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
                create_user(password=password, **form_data)
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
    app.run(debug=True)
