"""Print a short-lived native-local developer token for /docs; never for a deployed service.

uv run python scripts/local_token.py                  # the demo parent
uv run python scripts/local_token.py --editor         # editor 1 (author, release builder)
uv run python scripts/local_token.py --reviewer       # editor 2 (reviewer, publisher)
uv run python scripts/local_token.py --sub <uuid>     # another parent, e.g. to check isolation
"""

import argparse
from uuid import UUID

from dotenv import load_dotenv
from local_auth import EDITOR, PARENT, REVIEWER, demo_settings, local_token

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
role = parser.add_mutually_exclusive_group()
role.add_argument("--editor", action="store_true", help=f"editor token (default sub {EDITOR})")
role.add_argument("--reviewer", action="store_true", help=f"second editor token (default sub {REVIEWER})")
parser.add_argument("--sub", type=UUID, help="token subject; defaults depend on the role")


def minutes(value: str) -> int:
    if not value.isdigit() or not 1 <= int(value) <= 60:
        raise argparse.ArgumentTypeError("must be between 1 and 60")
    return int(value)


parser.add_argument("--minutes", type=minutes, default=15, help="lifetime, 1-60 (default 15)")
args = parser.parse_args()

load_dotenv()
settings = demo_settings()
default = REVIEWER if args.reviewer else EDITOR if args.editor else PARENT
print(local_token(settings, args.sub or default, editor=args.editor or args.reviewer, minutes=args.minutes))
