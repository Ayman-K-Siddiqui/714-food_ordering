Staff accounts use `Users.role = 'staff'` and a required `Users.restaurant_id`.
Existing users and public registrations default to `customer`; registration
does not accept a role or restaurant assignment from the browser.

Local development logins use the password `StaffPass123!`:

| Restaurant | Email | Restaurant ID |
| --- | --- | --- |
| Burger Joint | burger.staff@example.com | 1 |
| Pizza Palace | pizza.staff@example.com | 2 |
| Taco Town | taco.staff@example.com | 3 |

After pulling, start or rebuild the app from the repository root:

```sh
docker compose up -d --build
```

Staff fields and sample accounts are defined in `init.sql` for new databases.
The app does not migrate existing databases at startup.

If you already ran the project before staff access was added, run the following
**once** from the repository root after starting Docker. It adds the staff fields
and logins while preserving existing users and orders. Skip it for a fresh
database or a database that already has the staff fields (including the local
database used during development).

```sh
docker compose exec -T db sqlplus -s / as sysdba < migrations/001_staff_access.sql
```

Accounts are seeded only for sample restaurants already present in the database;
Taco Town's account requires restaurant ID 3.

After the command completes, restart the web service:

```sh
docker compose restart web
```

Staff land at `/kitchen`; customers land at `/`. Kitchen reads and status updates
require staff access and use the restaurant assignment loaded from the database,
ignoring any restaurant ID supplied by the browser. Staff navigation shows
Kitchen; customer navigation shows Restaurants, My Orders, and Cart.