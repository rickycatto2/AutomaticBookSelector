import copy
import datetime
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from core import Store, DEFAULT_PROFILE, AppError
from notifications import Notifications, DeliveryFailure, SMTPMailer
from test_core import FakeABS, item

BASE = datetime.datetime(2026, 10, 6, 12, tzinfo=datetime.timezone.utc)

class Mailer:
    configured = True
    recipient = 'reader@example.com'
    origin = 'https://selector.example.com'
    def __init__(self):
        self.messages = []
        self.failure = None
    def send(self, subject, body, message_id):
        self.messages.append((subject, body, message_id))
        if self.failure is not None:
            raise DeliveryFailure(self.failure)

class MoodEmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.abs = FakeABS()
        self.store = Store(pathlib.Path(self.temp.name) / 'test.db', self.abs)
        self.store.sync()
        self.mailer = Mailer()
        self.email = Notifications(self.store, self.mailer)
    def enable(self, mode='completion'):
        with patch('notifications.now', return_value=BASE.isoformat()):
            self.email.settings({'mode': mode, 'timezone': 'America/Chicago', 'quiet_start': 0, 'quiet_end': 0})
    def finish(self, book='one', at=BASE + datetime.timedelta(minutes=1)):
        self.abs.user['mediaProgress'].append({'libraryItemId': book, 'isFinished': True, 'progress': 1})
        with patch('core.now', return_value=at.isoformat()):
            self.store.sync()
    def test_kick_expires_without_mutating_lasting_profile_and_invalidates_cache(self):
        self.abs.items = [item('one', 'Warm', description='cozy lesbian romance'), item('two', 'Crime', description='psychological thriller')]
        self.store.sync()
        profile = copy.deepcopy(DEFAULT_PROFILE)
        profile.update(likes='thriller', preferred_authors='')
        profile['weights'] = {k: 0 for k in profile['weights']}
        self.store.profile(profile)
        with patch('core.now', return_value=BASE.isoformat()):
            self.store.kick({'text': 'cosy sapphic romance', 'duration_days': 7})
            picks = self.store.recommendations()['books']
            self.assertEqual(picks[0]['id'], 'one')
            self.assertEqual(picks[0]['fit_label'], 'For your current kick')
        with patch('core.now', return_value=(BASE + datetime.timedelta(days=8)).isoformat()):
            self.assertFalse(self.store.kick()['active'])
            self.assertEqual(self.store.recommendations()['books'][0]['id'], 'two')
        self.assertEqual(self.store.profile(), profile)
    def test_current_feedback_stops_training_after_kick_expires_or_changes(self):
        self.abs.items = [item('one', 'Read', description='uncanny surreal'), item('two', 'Strange', description='uncanny surreal'), item('three', 'Other', description='detective crime')]
        self.store.sync()
        with patch('core.now', return_value=BASE.isoformat()):
            self.store.kick({'text': 'anything', 'duration_days': 7})
            self.store.feedback({'book_id': 'one', 'rating': 5, 'enjoyed': 'uncanny surreal', 'scope': 'current'})
            active = {b['id']: b['score'] for b in self.store.recommendations()['books']}
        with patch('core.now', return_value=(BASE + datetime.timedelta(days=8)).isoformat()):
            expired = {b['id']: b['score'] for b in self.store.recommendations()['books']}
        self.assertGreater(active['two'], expired['two'])
        self.assertEqual(self.store.feedback_book('one')['feedback']['scope'], 'current')
    def test_review_correction_preserves_original_kick_after_mood_changes(self):
        with patch('core.now', return_value=BASE.isoformat()):
            self.store.kick({'text': 'cozy', 'duration_days': 7})
            self.store.feedback({'book_id': 'one', 'rating': 4, 'notes': 'Original note', 'scope': 'current', 'dnf': True})
        original = self.store.feedback_book('one')['feedback']
        with patch('core.now', return_value=(BASE + datetime.timedelta(days=8)).isoformat()):
            self.store.kick({'text': 'thrillers', 'duration_days': 7})
            self.store.feedback(original | {'dnf': False})
        edited = self.store.feedback_book('one')['feedback']
        self.assertEqual(edited['kick_context'], original['kick_context'])
        self.assertEqual(edited['dnf'], 0)
        self.assertEqual(edited['notes'], original['notes'])
        self.store.feedback(edited | {'scope': 'lasting'})
        self.assertEqual(self.store.feedback_book('one')['feedback']['kick_context'], '')

    def test_invalid_kick_scope_and_email_settings_are_rejected(self):
        with self.assertRaises(AppError): self.store.kick({'text': 'x', 'duration_days': -1})
        with self.assertRaises(AppError): self.store.feedback({'book_id': 'one', 'scope': 'bad'})
        with self.assertRaises(AppError): self.email.settings({'mode': 'weekly', 'timezone': 'bogus'})
        self.mailer.configured = False
        with self.assertRaises(AppError): self.enable()
    def test_off_is_default_and_completion_is_once_after_repeated_sync(self):
        self.finish(at=BASE - datetime.timedelta(days=1))
        self.email.process(BASE + datetime.timedelta(minutes=2))
        self.assertEqual(self.mailer.messages, [])
        self.enable()
        self.finish('two')
        self.email.process(BASE + datetime.timedelta(minutes=3))
        self.store.sync()
        self.email.process(BASE + datetime.timedelta(days=8))
        self.assertEqual(len(self.mailer.messages), 2)  # New book, then old backlog once.
        self.email.process(BASE + datetime.timedelta(days=16))
        self.assertEqual(len(self.mailer.messages), 2)
    def test_weekly_waits_seven_days_and_has_direct_book_links(self):
        self.enable('weekly')
        self.finish()
        self.finish('two')
        self.email.process(BASE + datetime.timedelta(days=6))
        self.assertEqual(self.mailer.messages, [])
        self.email.process(BASE + datetime.timedelta(days=7))
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertIn('/#feedback=one', self.mailer.messages[0][1])
        self.assertIn('/#feedback=two', self.mailer.messages[0][1])
        self.assertNotIn('fake-token', self.mailer.messages[0][1])
    def test_feedback_dismissal_and_reset_completions_are_not_emailed(self):
        self.enable()
        for book in ('one', 'two', 'three'): self.finish(book)
        self.store.feedback({'book_id': 'one', 'notes': 'Good'})
        self.store.dismiss_question('two')
        self.abs.user['mediaProgress'] = []
        self.store.sync()
        self.email.process(BASE + datetime.timedelta(minutes=3))
        self.assertEqual(self.mailer.messages, [])
    def test_quiet_hours_defer_until_morning_in_chicago(self):
        self.enable()
        self.email.settings({'mode': 'completion', 'timezone': 'America/Chicago', 'quiet_start': 21, 'quiet_end': 9})
        self.finish()
        self.email.process(BASE + datetime.timedelta(hours=15))  # 10pm Chicago
        self.assertEqual(self.mailer.messages, [])
        self.email.process(BASE + datetime.timedelta(days=1, hours=3))  # 10am
        self.assertEqual(len(self.mailer.messages), 1)
    def test_ambiguous_delivery_is_not_retried_even_after_restart(self):
        self.enable()
        self.finish()
        self.mailer.failure = True
        self.email.process(BASE + datetime.timedelta(minutes=3))
        self.mailer.failure = None
        new_store = Store(self.store.path, self.abs)
        Notifications(new_store, self.mailer).process(BASE + datetime.timedelta(days=8))
        self.assertEqual(len(self.mailer.messages), 1)
        self.assertEqual(self.email.settings()['uncertain_batches'], 1)
    def test_known_failure_waits_an_hour_then_retries(self):
        self.enable()
        self.finish()
        self.mailer.failure = False
        self.email.process(BASE + datetime.timedelta(minutes=3))
        self.mailer.failure = None
        self.email.process(BASE + datetime.timedelta(minutes=30))
        self.assertEqual(len(self.mailer.messages), 1)
        self.email.process(BASE + datetime.timedelta(hours=2))
        self.assertEqual(len(self.mailer.messages), 2)
    def test_disabled_persists_and_credentials_never_appear_in_settings(self):
        self.enable()
        self.email.settings({'mode': 'off', 'timezone': 'America/Chicago', 'quiet_start': 0, 'quiet_end': 0})
        self.finish()
        self.email.process(BASE + datetime.timedelta(days=8))
        self.assertEqual(self.mailer.messages, [])
        self.assertNotIn('password', self.email.settings())
    def test_smtp_requires_tls_and_valid_addresses(self):
        with patch.dict('os.environ', {'SMTP_HOST': 'smtp.example.com', 'SMTP_USERNAME': 'u', 'SMTP_PASSWORD': 'secret', 'SMTP_FROM': 'a@example.com', 'EMAIL_TO': 'b@example.com', 'APP_PUBLIC_ORIGIN': 'https://selector.example.com', 'SMTP_SECURITY': 'starttls'}):
            self.assertTrue(SMTPMailer().configured)
            with patch.dict('os.environ', {'SMTP_SECURITY': 'none'}): self.assertFalse(SMTPMailer().configured)
            with patch.dict('os.environ', {'EMAIL_TO': 'b@example.com\nBcc:evil@example.com'}): self.assertFalse(SMTPMailer().configured)

    def test_existing_database_feedback_survives_migration(self):
        import sqlite3
        path = pathlib.Path(self.temp.name) / 'legacy.db'
        with sqlite3.connect(path) as db:
            db.executescript('''CREATE TABLE feedback (book_id TEXT PRIMARY KEY,rating INTEGER,notes TEXT,enjoyed TEXT,avoid TEXT,dnf INTEGER,dismissed INTEGER,updated_at TEXT);
                INSERT INTO feedback VALUES ('one',5,'Keep this note','voice','',0,0,'old');
                CREATE TABLE completion_questions (book_id TEXT PRIMARY KEY,finished_at TEXT,dismissed INTEGER DEFAULT 0);
                INSERT INTO completion_questions VALUES ('one','old',0);''')
        db.close()
        legacy = Store(path, self.abs)
        with legacy.db() as db:
            row = dict(db.execute('SELECT * FROM feedback').fetchone())
            self.assertEqual(row['notes'], 'Keep this note')
            self.assertEqual(row['scope'], 'lasting')
            self.assertTrue(db.execute('SELECT created_at FROM completion_questions').fetchone()[0])

if __name__ == '__main__': unittest.main()
