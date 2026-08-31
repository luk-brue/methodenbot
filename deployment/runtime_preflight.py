#!/usr/bin/env python3
"""Secret-free output preflight for a staged final Methodenbot release."""

import argparse
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.offline == args.live:
        raise RuntimeError('choose_exactly_one_mode')

    from configuration import Configuration
    config = Configuration()
    config.validate_final_runtime()
    print('configuration=ok')
    if args.offline:
        return

    if os.geteuid() == 0:
        raise RuntimeError('live_preflight_must_run_as_service_user')
    from ai_service import AISummaryService
    import exchangemail
    from manual_delivery import select_latest_requests
    from matrixbot import MatrixBot
    from matrix_commands import validate_control_room
    import requests

    ai = AISummaryService(config.ai)
    if config.ai.api_key or not config.ai.api_key_file:
        raise RuntimeError('local_credential_file_required')
    token = config.ai.read_key()
    with requests.Session() as session:
        session.trust_env = False
        response = session.get('http://127.0.0.1:18765/v1/models',
                               headers={'Authorization': 'Bearer ' + token},
                               timeout=(5, 20), allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 1_000_000:
            raise RuntimeError('local_gateway_unavailable')
        body = response.json()
        identifiers = {item.get('id') for item in body.get('data', []) if isinstance(item, dict)}
        if config.ai.model not in identifiers:
            raise RuntimeError('configured_model_missing')
    print('local_gateway_and_model=ok')

    bot = MatrixBot(config)
    bot.token_whoami()
    validate_control_room(bot, config)
    print('matrix_identity_and_control_room=ok')

    exchange = exchangemail.init_exchange_connection(config)
    items = select_latest_requests(exchange, 3)
    for item in items:
        exchangemail.parse_email_data(item)
    print('exchange_latest_three_readonly=ok')

    ai.check_available()
    print('ai_local_token=ok')


if __name__ == '__main__':
    main()
