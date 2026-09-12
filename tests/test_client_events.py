"""Phase 9: the per-client change log behind the SSE fast path.

The stream carries a revision nudge for correctness; this log is what lets a second tab
patch the one card that moved instead of fetching a delta.
"""
import unittest

from panel.core import client_events


class ClientEventLogTests(unittest.TestCase):
    def setUp(self):
        client_events.reset()
        self.addCleanup(client_events.reset)

    def test_it_replays_events_newer_than_a_revision_oldest_first(self):
        client_events.record(7, client_id='u1', email='bob', revision=5, operation='renew',
                             client_state={'email': 'bob', 'total_bytes': 1})
        client_events.record(7, client_id='u1', email='bob', revision=6, operation='renew',
                             client_state={'email': 'bob', 'total_bytes': 2})

        events = client_events.since(4)
        self.assertEqual([event['revision'] for event in events], [5, 6])
        self.assertEqual(events[1]['client_state']['total_bytes'], 2)
        self.assertEqual(events[1]['operation'], 'renew')
        self.assertEqual(events[1]['email'], 'bob')

        self.assertEqual([event['revision'] for event in client_events.since(5)], [6])
        self.assertEqual(client_events.since(6), [])

    def test_a_viewer_without_a_revision_gets_no_replay(self):
        client_events.record(7, revision=5)
        self.assertEqual(client_events.since(None), [])
        self.assertEqual(client_events.since(0), [])
        self.assertEqual(client_events.since('nonsense'), [])

    def test_the_log_is_bounded_to_the_newest_events(self):
        total = client_events.MAX_EVENTS + 49
        for revision in range(1, total + 1):
            client_events.record(7, revision=revision)

        events = client_events.since(1)
        self.assertEqual(len(events), client_events.MAX_EVENTS)
        self.assertEqual(events[0]['revision'], total - client_events.MAX_EVENTS + 1)

    def test_an_unverified_write_carries_no_state(self):
        event = client_events.record(7, email='bob', revision=3, operation='update')
        self.assertIsNone(event['client_state'])
        self.assertFalse(event['deleted'])


if __name__ == '__main__':
    unittest.main()
