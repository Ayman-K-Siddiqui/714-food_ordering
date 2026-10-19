# Food Ordering

## Run locally

With Docker Desktop running:

```sh
git pull --ff-only
docker compose up -d --build
```

Open http://localhost:5001/. Compose waits for Oracle to become healthy before starting Flask. To inspect database startup:

```sh
docker logs -f oracle-db
```

The home page lists restaurants from `Restaurants`. Select a restaurant to view only its available menu items. `/menu` redirects to restaurant selection.

## Accounts and sessions

For a freshly initialized local database, sign in at `/login` with `test@example.com` and password `password`. This is a shared development test account seeded by `init.sql`. Existing databases can add it by running just the demo-user `INSERT` at the end of `init.sql`, followed by `COMMIT;`, without resetting other data.

You can also create an account at `/register`, then sign in at `/login`. Login checks the stored Werkzeug password hash in `Users`; it does not store a password in the session. Navigation displays the current user, and Log out clears the session. Forms include CSRF tokens.

The signed session contains `user_id`, which is validated against `Users` on each request. Future cart/order handlers should use `@login_required` and read `g.user["user_id"]` to determine ownership, rather than accepting a user ID from the browser. Cart actions are not implemented yet.

For local development, a signing key is generated in `instance/session.key` and reused across reloads and restarts. This directory is excluded from Git and Docker builds. Deployments can set `SECRET_KEY` in the Flask container environment; set `SESSION_COOKIE_SECURE=1` when serving over HTTPS. All app instances must use the same signing key. Sessions expire after eight hours of inactivity.

## Database setup

`init.sql` creates the schema and sample restaurants/menu items when Oracle initializes a fresh database. Each developer has their own local database. Existing databases require migrations or an explicit reset to apply schema changes.

For an intentional local reset, run `docker compose down -v` followed by `docker compose up -d --build`. This discards local database data. Ordinary startup does not require a reset.

## Tests

With the project's Python dependencies installed:

```sh
python -m unittest discover -s tests -v
```

The automated tests mock database calls and do not require Oracle.
