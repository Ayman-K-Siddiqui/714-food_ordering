import os
from collections import OrderedDict

import oracledb
from flask import Flask, render_template

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


if __name__ == "__main__":
    app.run(debug=True)
