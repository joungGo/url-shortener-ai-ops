"""TLS-only ASGI Events endpoint: authenticate/persist/ACK, never investigate or send."""
import asyncio
import json
import sqlite3
import time

from ai_ops.slack_adapter import BODY_LIMIT


class SlackEventsApp:
    ACK_SECONDS = 2.5
    MAX_INFLIGHT = 4
    PATH = '/slack/events'

    def __init__(self, adapter):
        if adapter.transport_mode != 'slack_web_api':
            raise ValueError('slack_web_adapter_required')
        self.adapter = adapter
        self._inflight = 0
        self._tasks = set()

    def _handle(self, headers, body):
        # Looked up per request (not bound at init) so the live adapter is used.
        return self.adapter.receive(headers, body)

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type':'lifespan.startup.complete'})
                elif event['type'] == 'lifespan.shutdown':
                    if self._tasks:
                        # Cancelling lifespan shutdown cannot cancel a to_thread
                        # wrapper: its actual thread may still persist the request.
                        await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._tasks)),
                                             return_exceptions=True)
                    await send({'type':'lifespan.shutdown.complete'})
                    return
        if scope['type'] != 'http':
            return
        started = time.monotonic()
        status, result = 403, {'status':'rejected'}
        reserved = False
        try:
            if (scope.get('scheme') != 'https' or scope.get('path') != self.PATH
                    or scope.get('method') != 'POST'):
                raise ValueError('rejected')
            if self._inflight >= self.MAX_INFLIGHT:
                raise OSError('busy')
            self._inflight += 1
            reserved = True
            headers = [(k.decode('ascii'),v.decode('latin1')) for k,v in scope.get('headers',[])]
            lengths = [v for k,v in headers if k.lower() == 'content-length']
            if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit()
                    or len(lengths[0]) > 6 or not 0 < int(lengths[0]) <= BODY_LIMIT
                    or any(k.lower() == 'transfer-encoding' for k,v in headers)):
                raise ValueError('rejected')
            expected = int(lengths[0])
            body = bytearray()
            while True:
                remaining = self.ACK_SECONDS - (time.monotonic()-started)
                if remaining <= 0:
                    raise TimeoutError('deadline')
                event = await asyncio.wait_for(receive(), remaining)
                if event.get('type') != 'http.request':
                    raise ValueError('disconnected')
                part = event.get('body',b'')
                if not isinstance(part,bytes) or len(body)+len(part) > expected:
                    raise ValueError('rejected')
                body.extend(part)
                if not event.get('more_body',False):
                    break
            if len(body) != expected:
                raise ValueError('rejected')
            remaining = self.ACK_SECONDS - (time.monotonic()-started)
            if remaining <= 0:
                raise TimeoutError('deadline')
            task = asyncio.create_task(asyncio.to_thread(self._handle,headers,bytes(body)))
            self._tasks.add(task)
            reserved = False  # release only when the thread has actually finished
            def finished(done):
                self._inflight -= 1
                self._tasks.discard(done)
                if not done.cancelled():
                    done.exception()  # consume errors after caller timeout/cancellation
            task.add_done_callback(finished)
            result = await asyncio.wait_for(asyncio.shield(task), remaining)
            if time.monotonic()-started >= self.ACK_SECONDS:
                raise TimeoutError('deadline')
            status = 200
        except (OSError, sqlite3.Error, asyncio.TimeoutError):
            status, result = 503, {'status':'retry_later'}
        except (ValueError, TypeError, KeyError, RecursionError):
            status, result = 403, {'status':'rejected'}
        except Exception:
            status, result = 503, {'status':'retry_later'}
        finally:
            if reserved:
                self._inflight -= 1
        headers = [(b'content-type',b'application/json; charset=utf-8'),(b'cache-control',b'no-store')]
        if status == 403:
            headers.append((b'x-slack-no-retry',b'1'))
        encoded = json.dumps(result,ensure_ascii=False).encode()
        headers.append((b'content-length',str(len(encoded)).encode()))
        await send({'type':'http.response.start','status':status,'headers':headers})
        await send({'type':'http.response.body','body':encoded})


class SlackInteractivityApp(SlackEventsApp):
    """Same TLS/size/2.5s-ACK/in-flight limits for signed Slack interactivity payloads."""
    PATH = '/slack/interactivity'

    def __init__(self, receiver):
        from ai_ops.recovery_slack_approval import SlackApprovalReceiver
        if not isinstance(receiver, SlackApprovalReceiver):
            raise ValueError('slack_approval_receiver_required')
        self.adapter = None
        self.receiver = receiver
        self._inflight = 0
        self._tasks = set()

    def _handle(self, headers, body):
        return self.receiver.receive(headers, body)
