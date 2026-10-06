"""Private SMTP reminders, with durable claims and no automatic ambiguous-delivery retries."""
import datetime
import email.utils
from email.message import EmailMessage
import os
import smtplib
import ssl
import threading
import urllib.parse
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core import AppError, now

DEFAULTS = {'mode': 'off', 'timezone': 'America/Chicago', 'quiet_start': 21, 'quiet_end': 9}

class DeliveryFailure(Exception):
    def __init__(self, uncertain=False):
        self.uncertain = uncertain

class SMTPMailer:
    def __init__(self):
        self.host = os.getenv('SMTP_HOST', '')
        self.port = os.getenv('SMTP_PORT', '587')
        self.username = os.getenv('SMTP_USERNAME', '')
        self.password = os.getenv('SMTP_PASSWORD', '')
        self.sender = os.getenv('SMTP_FROM', '')
        self.recipient = os.getenv('EMAIL_TO', '')
        self.security = os.getenv('SMTP_SECURITY', 'starttls')
        self.origin = os.getenv('APP_PUBLIC_ORIGIN', '').rstrip('/')

    @property
    def configured(self):
        try:
            origin = urllib.parse.urlsplit(self.origin)
            return bool(self.host and self.username and self.password and self.security in ('starttls', 'ssl')
                and 1 <= int(self.port) <= 65535 and origin.scheme == 'https' and origin.hostname
                and not origin.path and not origin.query and not origin.fragment and not origin.username
                and all('\n' not in address and '\r' not in address and email.utils.parseaddr(address)[1] == address
                        and address.count('@') == 1 for address in (self.sender, self.recipient)))
        except ValueError:
            return False

    def send(self, subject, body, message_id):
        if not self.configured:
            raise DeliveryFailure()
        message = EmailMessage()
        message['From'], message['To'], message['Subject'] = self.sender, self.recipient, subject
        message['Date'] = email.utils.formatdate(localtime=False)
        message['Message-ID'] = '<' + message_id + '@' + urllib.parse.urlsplit(self.origin).hostname + '>'
        message.set_content(body)
        smtp, submitting = None, False
        try:
            context = ssl.create_default_context()
            if self.security == 'ssl':
                smtp = smtplib.SMTP_SSL(self.host, int(self.port), timeout=25, context=context)
            else:
                smtp = smtplib.SMTP(self.host, int(self.port), timeout=25)
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(self.username, self.password)
            submitting = True
            smtp.send_message(message)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            raise DeliveryFailure(False) from None
        except Exception:
            raise DeliveryFailure(submitting) from None
        finally:
            if smtp:
                try:
                    smtp.close()
                except Exception:
                    pass

class Notifications:
    def __init__(self, store, mailer=None):
        self.store = store
        self.mailer = mailer or SMTPMailer()
        self.lock = threading.Lock()

    def settings(self, payload=None):
        with self.store.lock, self.store.db() as db:
            settings = self.store.meta(db, 'email_settings', DEFAULTS.copy())
            if payload is not None:
                mode = payload.get('mode')
                zone = payload.get('timezone', 'America/Chicago')
                start, end = payload.get('quiet_start', 21), payload.get('quiet_end', 9)
                if mode not in ('off', 'completion', 'weekly') or not isinstance(zone, str) or any(type(v) is not int or not 0 <= v <= 23 for v in (start, end)):
                    raise AppError('Choose valid email timing and quiet hours.')
                try:
                    ZoneInfo(zone)
                except (ZoneInfoNotFoundError, ValueError):
                    raise AppError('Use a time zone such as America/Chicago.') from None
                if mode != 'off' and not self.mailer.configured:
                    raise AppError('Configure the email provider in .env and restart before enabling reminders.')
                if settings['mode'] == 'off' and mode != 'off':
                    self.store.set_meta(db, 'email_enabled_at', now())
                    self.store.set_meta(db, 'email_next_weekly', (datetime.datetime.fromisoformat(now()) + datetime.timedelta(days=7)).isoformat())
                settings = {'mode': mode, 'timezone': zone, 'quiet_start': start, 'quiet_end': end}
                self.store.set_meta(db, 'email_settings', settings)
            uncertain = db.execute("SELECT count(*) FROM email_batches WHERE status='uncertain'").fetchone()[0]
            return settings | {'configured': self.mailer.configured, 'recipient': self.mailer.recipient if self.mailer.configured else '',
                'last_sent': self.store.meta(db, 'email_last_sent'), 'error': self.store.meta(db, 'email_error'),
                'next_weekly': self.store.meta(db, 'email_next_weekly'), 'uncertain_batches': uncertain}

    def pending(self, db):
        return [dict(r) for r in db.execute('''SELECT b.id,b.title,b.author,q.created_at
            FROM books b JOIN completion_questions q ON b.id=q.book_id
            LEFT JOIN email_receipts e ON b.id=e.book_id
            WHERE b.finished=1 AND q.dismissed=0 AND NOT EXISTS(SELECT 1 FROM feedback f WHERE f.book_id=b.id)
            AND (e.book_id IS NULL OR e.status='failed') ORDER BY q.created_at,b.id''')]

    def content(self, books, weekly=False):
        subject = 'A moment for your finished books' if weekly or len(books) != 1 else 'You finished a book — how did it feel?'
        lines = ['A note from AutomaticBookSelector', '', 'What worked for you — and what would you like more of?', '']
        for book in books:
            lines += [book['title'] + ' — ' + book['author'], self.mailer.origin + '/#feedback=' + urllib.parse.quote(book['id'], safe=''), '']
        lines += ['See all books waiting for feedback:', self.mailer.origin + '/#finished', '',
                  'Sign in with your usual private email login. No feedback is saved by opening a link.',
                  'Change or turn off reminders in History & connection:', self.mailer.origin + '/#settings']
        return subject, '\n'.join(lines)

    def test(self):
        if not self.mailer.configured:
            raise AppError('Configure SMTP settings in .env and restart first.')
        try:
            self.mailer.send('Your reading companion email is connected', 'Your feedback link:\n' + self.mailer.origin + '/#finished', uuid.uuid4().hex)
        except DeliveryFailure:
            raise AppError('Delivery could not be confirmed. Check the provider settings and your inbox before retrying.') from None
        return {'sent': True}

    def process(self, at=None):
        if not self.lock.acquire(blocking=False):
            return
        try:
            instant = at or datetime.datetime.now(datetime.timezone.utc)
            stamp = instant.isoformat()
            settings = self.settings()
            if settings['mode'] == 'off' or not settings['configured']:
                return
            hour = instant.astimezone(ZoneInfo(settings['timezone'])).hour
            start, end = settings['quiet_start'], settings['quiet_end']
            if start != end and (start <= hour < end if start < end else hour >= start or hour < end):
                return
            with self.store.lock, self.store.db() as db:
                if self.store.sync_state['running'] or not self.store.meta(db, 'last_sync') or self.store.sync_state['error']:
                    return
                if self.store.meta(db, 'email_retry_after', '') > stamp:
                    return
                books = self.pending(db)
                weekly = self.store.meta(db, 'email_next_weekly', stamp) <= stamp
                if not weekly:
                    if settings['mode'] == 'weekly':
                        return
                    enabled = self.store.meta(db, 'email_enabled_at', stamp)
                    books = [b for b in books if b['created_at'] > enabled]
                books = books[:20]
                if not books:
                    if weekly:
                        self.store.set_meta(db, 'email_next_weekly', (instant + datetime.timedelta(days=7)).isoformat())
                    return
                batch = uuid.uuid4().hex
                db.execute("INSERT INTO email_batches VALUES (?,'sending',?,NULL)", (batch, stamp))
                for b in books:
                    db.execute("INSERT INTO email_receipts VALUES (?,?,'sending',?) ON CONFLICT(book_id) DO UPDATE SET batch_id=excluded.batch_id,status='sending',attempted_at=excluded.attempted_at", (b['id'], batch, stamp))
            subject, body = self.content(books, weekly)
            outcome = 'sent'
            try:
                self.mailer.send(subject, body, batch)
            except DeliveryFailure as error:
                outcome = 'uncertain' if error.uncertain else 'failed'
            with self.store.lock, self.store.db() as db:
                db.execute('UPDATE email_batches SET status=?,completed_at=? WHERE id=?', (outcome, stamp, batch))
                db.execute('UPDATE email_receipts SET status=? WHERE batch_id=?', (outcome, batch))
                if outcome == 'sent':
                    self.store.set_meta(db, 'email_last_sent', stamp)
                    self.store.set_meta(db, 'email_error', None)
                    if weekly:
                        self.store.set_meta(db, 'email_next_weekly', (instant + datetime.timedelta(days=7)).isoformat())
                else:
                    self.store.set_meta(db, 'email_error', 'Delivery is uncertain; no automatic resend.' if outcome == 'uncertain' else 'Email delivery failed. Check SMTP settings; another attempt will wait at least an hour.')
                    self.store.set_meta(db, 'email_retry_after', (instant + datetime.timedelta(hours=1)).isoformat())
        finally:
            self.lock.release()
