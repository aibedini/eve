import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_eve_gmweb_consumer_matches_declared_contract():
    contract = json.loads(
        (ROOT / 'shared' / 'eve-gmweb-contract-v1.json').read_text(encoding='utf-8')
    )
    assert contract['consumer'] == 'eve'
    assert contract['projectKeyDefaults']['scopes'] == [
        'sms.send', 'sms.status', 'sms.cancel', 'sms.capacity',
    ]
    source = (ROOT / 'panel' / 'jobs' / 'messaging.py').read_text(encoding='utf-8')
    for fragment in (
        '/send/capacity', '/send",', '/send/cancel/', '/send/status/',
        'Idempotency-Key',
    ):
        assert fragment in source
