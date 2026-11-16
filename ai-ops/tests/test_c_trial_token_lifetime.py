"""설치 토큰의 서로 다른 시계·전송 근거·사용 기한과 실패 회수 회귀."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
import traceback
import unittest
from unittest.mock import Mock, patch

from ai_ops import c_trial_token_lifetime as lifetime
from ai_ops import c_trial_writer_api as writer
from ai_ops.c_trial_github import GitHubMergeSession, INSTALLATION_ID
from test_c_trial_github import Session

NOW = datetime(2026, 11, 16, 13, 30, tzinfo=timezone.utc)


def response(*, offset=1, seconds=3600, elapsed=.2, dates=None, expiry=None):
    server = NOW+timedelta(seconds=offset)
    exchange = lifetime.TokenExchange(NOW-timedelta(seconds=elapsed), 100-elapsed,
        NOW, 100., dates if dates is not None else (format_datetime(server, usegmt=True),))
    return lifetime.TokenResponse({'token': 'synthetic-token-not-a-secret',
        'permissions': writer.PERMISSIONS,
        'expires_at': expiry if expiry is not None else (server+timedelta(seconds=seconds)).isoformat()}, exchange)


class TokenTimeTest(unittest.TestCase):
    def setUp(self):
        self.tick, self.wall = 100., NOW
        self.addCleanup(patch.stopall)
        patch.object(lifetime.time, 'monotonic', side_effect=lambda:self.tick).start()
        patch.object(lifetime, 'utcnow', side_effect=lambda:self.wall).start()

    def test_normal_hour_with_one_second_clock_difference_passes_without_extending_hour(self):
        issued = response()
        # 수정 전 실제 작성/병합 식은 서버가 1초 앞선 정상 토큰을 거절한다.
        old_seconds = (datetime.fromisoformat(issued['expires_at'])-NOW).total_seconds()
        self.assertEqual(old_seconds, 3601)
        self.assertFalse(0 < old_seconds <= 3600)
        valid = lifetime.validate_token_lifetime(issued)
        self.assertEqual(valid.diagnostic['server_remaining_seconds'], 3600)
        self.assertLess(valid.deadline-self.tick, 3600)
        self.assertAlmostEqual(valid.deadline-self.tick, 3598.8)
        self.assertNotIn(issued['token'], json.dumps(valid.diagnostic))

    def test_server_relative_hour_cap_rejects_even_when_old_local_check_passed(self):
        issued = response(offset=-2, seconds=3601)
        old_seconds = (datetime.fromisoformat(issued['expires_at'])-NOW).total_seconds()
        self.assertTrue(0 < old_seconds <= 3600)
        with self.assertRaisesRegex(lifetime.TokenLifetimeRejected, '^github_token_server_lifetime_rejected$'):
            lifetime.validate_token_lifetime(issued)

    def test_expired_and_too_little_conservative_lifetime_are_refused(self):
        for seconds in (-1, 0, 1):
            with self.subTest(seconds=seconds), self.assertRaises(lifetime.TokenLifetimeRejected):
                lifetime.validate_token_lifetime(response(offset=0, seconds=seconds))

    def test_large_clock_skew_is_not_a_new_ttl_allowance(self):
        for offset in (-6, 6):
            with self.subTest(offset=offset), self.assertRaisesRegex(
                    lifetime.TokenLifetimeRejected, '^github_token_clock_skew_exceeded$'):
                lifetime.validate_token_lifetime(response(offset=offset))

    def test_missing_duplicate_malformed_and_body_forged_date_are_refused(self):
        for dates in ((), ('bad-secret-header',), (format_datetime(NOW, usegmt=True),)*2):
            with self.subTest(dates=dates), self.assertRaises(lifetime.TokenLifetimeRejected) as cm:
                lifetime.validate_token_lifetime(response(dates=dates))
            self.assertNotIn('bad-secret-header', json.dumps(cm.exception.diagnostic))
        body = dict(response()); body['exchange'] = response().exchange
        with self.assertRaisesRegex(lifetime.TokenLifetimeRejected, '^github_token_time_evidence_missing$'):
            lifetime.validate_token_lifetime(body)

    def test_invalid_expiry_cannot_leak_body_or_disable_revocation(self):
        for expiry in ('secret-expiry', '2026-11-16T14:30:00', '2026-11-16T14:30:00+01:00', None):
            issued = response(); issued['expires_at'] = expiry
            with self.subTest(expiry=expiry), self.assertRaises(lifetime.TokenLifetimeRejected) as cm:
                lifetime.validate_token_lifetime(issued)
            self.assertNotIn('secret-expiry', json.dumps(cm.exception.diagnostic))
            self.assertNotIn('secret-expiry', ''.join(traceback.format_exception(cm.exception)))

    def test_wall_clock_jump_during_issue_and_long_exchange_are_refused(self):
        issued = response(); x = issued.exchange
        issued.exchange = lifetime.TokenExchange(x.sent_at, x.sent_tick,
            x.received_at-timedelta(seconds=2), x.received_tick, x.dates)
        with self.assertRaisesRegex(lifetime.TokenLifetimeRejected, '^github_token_clock_changed$'):
            lifetime.validate_token_lifetime(issued)
        with self.assertRaisesRegex(lifetime.TokenLifetimeRejected, '^github_token_exchange_time_rejected$'):
            lifetime.validate_token_lifetime(response(elapsed=13))

    def test_monotonic_expiry_and_wall_clock_jump_never_extend_use(self):
        valid = lifetime.validate_token_lifetime(response())
        self.tick = valid.deadline; self.wall += timedelta(seconds=self.tick-100)
        with self.assertRaisesRegex(lifetime.TokenLifetimeRejected, '^github_token_expired$'):
            valid.require_current()
        self.tick = 101.; self.wall = NOW-timedelta(seconds=2)
        with self.assertRaisesRegex(lifetime.TokenLifetimeRejected, '^github_token_clock_changed$'):
            valid.require_current()

    def test_writer_uses_exact_existing_scope_and_revokes_after_valid_use(self):
        calls, reports = [], []
        def api(method, path, credential, payload=None):
            calls.append((method, path, payload))
            return response() if path.endswith('/access_tokens') else {}
        with patch.object(writer, 'api', side_effect=api), patch.object(writer, 'jwt', side_effect=AssertionError('key-read')):
            with writer.installation_token('synthetic-jwt', report=reports.append) as grant:
                grant.api('GET', '/installation/repositories?per_page=100')
        self.assertEqual(calls[0][2], {'repository_ids':[writer.REPO_ID], 'permissions':writer.PERMISSIONS})
        self.assertEqual(calls[-1][:2], ('DELETE', '/installation/token'))
        self.assertEqual(reports[0]['status'], 'validated')
        with self.assertRaisesRegex(ValueError, '^github_writer_token_closed$'):
            grant.api('POST', '/repos/joungGo/c03-execution-lab/git/refs', {})

    def test_writer_bad_lifetime_revokes_before_repository_or_pr_write(self):
        reports=[]
        for issued in (response(seconds=3601), response(dates=()), response(seconds=0)):
            calls=[]
            def api(method,path,credential,payload=None):
                calls.append((method,path)); return issued if path.endswith('/access_tokens') else {}
            with patch.object(writer,'api',side_effect=api), self.assertRaises(lifetime.TokenLifetimeRejected):
                with writer.installation_token('synthetic',report=reports.append):
                    self.fail('invalid grant yielded')
            self.assertEqual([m for m,_ in calls], ['POST','DELETE'])
        self.assertTrue(all(r['status']=='rejected' for r in reports))

    def test_writer_expiry_cancellation_and_reporting_failure_still_revoke(self):
        for mode in ('expiry','cancel','report'):
            calls=[]
            def api(method,path,credential,payload=None):
                calls.append((method,path)); return response() if path.endswith('/access_tokens') else {}
            def report(value):
                if mode=='report': raise OSError('synthetic storage failure')
            with self.subTest(mode=mode), patch.object(writer,'api',side_effect=api):
                with self.assertRaises((lifetime.TokenLifetimeRejected,KeyboardInterrupt,OSError)):
                    with writer.installation_token('synthetic',report=report) as grant:
                        if mode=='cancel': raise KeyboardInterrupt()
                        self.tick=grant.lifetime.deadline; self.wall=NOW+timedelta(seconds=self.tick-100)
                        grant.api('POST','/repos/joungGo/c03-execution-lab/git/refs',{})
                self.assertEqual([m for m,_ in calls], ['POST','DELETE'])
                self.tick,self.wall=100.,NOW

    def test_merge_session_uses_same_valid_hour_and_revokes_rejected_hour(self):
        class TimedSession(Session):
            def _api(inner,method,path,credential,payload=None):
                if path.endswith('/access_tokens'):
                    inner.calls.append((method,path)); return inner.issued
                return super()._api(method,path,credential,payload)
        session=TimedSession(); session.issued=response()
        # 병합 App의 권한은 작성 App보다 좁으며 그 계약을 그대로 대조한다.
        from ai_ops.c_trial_github import PERMISSIONS
        session.issued['permissions']=PERMISSIONS
        with session: self.assertIsNotNone(session.lifetime)
        self.assertIsNone(session.token)
        session.issued=response(seconds=3601); session.issued['permissions']=PERMISSIONS
        with self.assertRaises(lifetime.TokenLifetimeRejected): session.__enter__()
        self.assertEqual(session.calls[-1], ('DELETE','/installation/token'))
        self.assertIsNone(session.token)

    def test_actual_merge_api_stops_expired_token_before_network_but_allows_delete(self):
        session=GitHubMergeSession(); session.token='synthetic'; session.lifetime=lifetime.validate_token_lifetime(response())
        self.tick=session.lifetime.deadline; self.wall+=timedelta(seconds=self.tick-100)
        with patch('ai_ops.c_trial_github.http.client.HTTPSConnection') as factory:
            with self.assertRaises(lifetime.TokenLifetimeRejected): session._api('GET','/app',session.token)
            factory.assert_not_called()
            conn=factory.return_value; conn.getresponse.return_value.status=204
            conn.getresponse.return_value.read.return_value=b''
            session.close()
            self.assertEqual(conn.request.call_args.args[0],'DELETE')

    def test_both_real_transports_attach_only_same_response_date_and_preserve_revoke(self):
        for module, call in ((writer, writer.api),
                (__import__('ai_ops.c_trial_github',fromlist=['GitHubMergeSession']),GitHubMergeSession()._api)):
            conn=Mock(); reply=conn.getresponse.return_value; reply.status=201
            reply.read.return_value=json.dumps(dict(response())).encode()
            reply.getheaders.return_value=[('Date',format_datetime(NOW+timedelta(seconds=1),usegmt=True)),
                                           ('X-Private','do-not-copy')]
            with self.subTest(module=module.__name__), patch.object(module,'utcnow',side_effect=lambda:self.wall), \
                    patch.object(module.http.client,'HTTPSConnection',return_value=conn):
                issued=call('POST',f'/app/installations/{INSTALLATION_ID}/access_tokens','synthetic-jwt',{})
            valid=lifetime.validate_token_lifetime(issued)
            self.assertEqual(valid.diagnostic['server_remaining_seconds'],3600)
            self.assertNotIn('do-not-copy',json.dumps(valid.diagnostic))
            conn.close.assert_called_once()
