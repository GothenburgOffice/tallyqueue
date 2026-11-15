# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gothenburg Office
"""python -m tallyqueue          run the server
python -m tallyqueue token NAME  make an access token for an approved user
"""

import argparse


def main():
    parser = argparse.ArgumentParser(prog='python -m tallyqueue', description='TallyQueue server.')
    commands = parser.add_subparsers(dest='command')
    commands.add_parser('serve', help='run the server (the default)')
    token = commands.add_parser('token', help='make an access token for an approved user')
    token.add_argument(
        'name', nargs='?', default='owner', help='user name: a-z, 0-9, - and _ (default: owner)'
    )
    args = parser.parse_args()

    if args.command == 'token':
        from . import auth

        if not auth.USER_RE.fullmatch(args.name):
            parser.error('the name is 1 to 32 characters: a-z, 0-9, - and _, starting with a letter or digit')
        value = auth.new_token()
        print(f'Token for {args.name}. It is shown only now: keep it in a password manager.\n')
        print(f'  {value}\n')
        print('Add this pair to TALLYQUEUE_USERS (comma-separated when there are several users):\n')
        print(f'  {args.name}:{auth.hash_token(value)}')
        return

    from .server import main as serve

    serve()


if __name__ == '__main__':
    main()
