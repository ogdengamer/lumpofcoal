"""App users from the command line: the way in when nobody can sign in.

    python -m app.users list
    python -m app.users invite NAME [--email EMAIL] [--url https://gifts.example.com]

`invite` makes a one-time sign-in link (AUTH_MODE=password) for NAME, giving them a
login first if they need one (--email). The person is created if they don't exist
yet. Run it inside the container:

    docker compose exec lumpofcoal python -m app.users invite "Sam" --email sam@example.com
"""

import argparse
import os
import sys

from sqlalchemy import select

from app import logins
from app.db import make_engine, make_sessionmaker, migrate
from app.gifts import FormError
from app.models import Person


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", "/data"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="who can sign in")
    invite = commands.add_parser("invite", help="make a one-time sign-in link")
    invite.add_argument("name")
    invite.add_argument("--email", help="their sign-in email, if they don't have a login yet")
    invite.add_argument("--url", default="", help="this app's address, to print a full link")
    args = parser.parse_args(argv)

    engine = make_engine(args.data_dir)
    migrate(engine)
    with make_sessionmaker(engine)() as session:
        if args.command == "list":
            for person in logins.app_users(session):
                state = "has a password" if person.password_hash else "no password yet"
                print(f"{person.name}: {person.login_email} ({state})", file=out)
            return 0

        person = session.scalar(select(Person).where(Person.name == args.name))
        if person is None:
            if not args.email:
                print(f"Nobody called {args.name!r}. Add --email to create them.", file=out)
                return 1
            person = Person(name=args.name)
            session.add(person)
        if args.email:
            try:
                logins.give_login(session, person, args.email)
            except FormError as exc:
                print(exc, file=out)
                return 1
        if person.login_email is None:
            print(f"{person.name} has no login yet. Add --email.", file=out)
            return 1
        session.flush()
        token = logins.create_invite(session, person, created_by_id=None)
        session.commit()
        path = f"/invite/{token}"
        print(f"Sign-in link for {person.name} ({person.login_email}), good for "
              f"{logins.INVITE_DAYS} days:", file=out)
        print(f"  {args.url.rstrip('/')}{path}" if args.url else
              f"  https://<this app's address>{path}", file=out)
        if os.environ.get("AUTH_MODE", "cloudflare") != "password":
            print("(Only used with AUTH_MODE=password; Cloudflare mode signs in through Access.)",
                  file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
