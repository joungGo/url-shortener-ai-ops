"""Opt-in wiring to the EXISTING task service, queue and separately running Worker.

No settings reload, worker launch, network call or recovery execution on import.
Factory construction reads private credentials only after the explicit enable gate.
"""
from pathlib import Path
from typing import Literal

from pydantic import Field, StrictBool, StrictStr, model_validator

from ai_ops.contracts import StrictModel
from ai_ops.job_queue import QueueBusy, QueueFull
from ai_ops.slack_adapter import SlackAdapter
from ai_ops.slack_ingress import SlackEventsApp
from ai_ops.slack_web import SlackWebTransport, read_private_secret
from ai_ops.workflow_config import read_config


class EventSource(StrictModel):
    source_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')
    cluster_id: StrictStr = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')


class SlackWebConfig(StrictModel):
    schema_version: Literal[1] = 1
    enabled: StrictBool = False
    bot_token_file: StrictStr
    signing_secret_file: StrictStr
    event_sources: dict[StrictStr, EventSource] = Field(default_factory=dict, max_length=32)

    @model_validator(mode='after')
    def sources_valid(self):
        from ai_ops.event_store import _name
        for alias in self.event_sources:
            _name(alias)
        pairs = [(s.source_id,s.cluster_id) for s in self.event_sources.values()]
        if len(pairs) != len(set(pairs)):
            raise ValueError('duplicate logical event source')
        return self

    def sources(self):
        return {alias:source.model_dump() for alias,source in self.event_sources.items()}

    @classmethod
    def load(cls, path):
        try:
            config = cls.model_validate(read_config(Path(path)))
            if any(not Path(v).is_absolute() for v in (config.bot_token_file,config.signing_secret_file)):
                raise ValueError('invalid')
            return config
        except (OSError, ValueError, TypeError, RecursionError):
            raise ValueError('slack_web_config_rejected') from None


class SlackRuntime:
    def __init__(self, tasks, policy, config, *, connection_factory=None, monotonic=None):
        if not isinstance(config,SlackWebConfig) or config.enabled is not True:
            raise ValueError('slack_web_disabled')
        if tasks.settings.runtime.llm_enabled:
            raise ValueError('slack_model_not_enabled')
        # Same TaskService object/config/queue as the existing worker deployment.
        self.tasks = tasks
        self._cursor = (-1, "")
        kwargs = {} if monotonic is None else {'monotonic':monotonic}
        self.transport = SlackWebTransport(policy,config.bot_token_file,enabled=True,
            connection_factory=connection_factory,**kwargs)
        secret = read_private_secret(config.signing_secret_file)
        self.adapter = SlackAdapter(tasks,policy,secret,transport_mode='slack_web_api',event_sources=config.sources())
        self.app = SlackEventsApp(self.adapter)

    def prepare(self):
        """Explicit external connection, to be called only after actual connection approval."""
        return self.transport.verify_identity()

    def relay_once(self, *, stopped=lambda: False):
        """Bounded inbox relay + one ready reply; existing Worker runs separately.

        No retries of ambiguous sends. A failed identity refresh prevents posting.
        Pending investigations are skipped so they cannot starve completed work.
        """
        if stopped():
            return {'state':'stopped','execution_allowed':False}
        if not self.transport.verified_for(self.adapter.policy):
            self.prepare()
        if stopped():
            return {'state':'stopped','execution_allowed':False}
        try:
            admitted = self.adapter.drain_one()
        except (QueueBusy, QueueFull):
            admitted = {'state':'backpressure'}
        with self.adapter._db() as db:
            candidates = db.execute('SELECT r.request_id,r.job_id,r.created_at FROM slack_requests r '
                'LEFT JOIN slack_replies p ON r.request_id=p.request_id '
                'WHERE r.state IN ("queued","ready") AND (p.request_id IS NULL OR '
                '(p.state="pending" AND p.retry_at<=? AND p.attempts<3)) '
                'AND (r.created_at,r.request_id)>(?,?) ORDER BY r.created_at,r.request_id LIMIT 100',
                (self.tasks.queue.now(),*self._cursor)).fetchall()
        # Page across pending jobs; at most 100 lookups and one reply per tick.
        if not candidates:
            self._cursor = (-1, "")
        for row in candidates:
            if stopped():
                return {'state':'stopped','execution_allowed':False}
            self._cursor = (row['created_at'],row['request_id'])
            try:
                if row['job_id'] and self.tasks.show(row['job_id'])['result']['state'] in ('pending','expired'):
                    continue
            except (ValueError, OSError):
                # The existing authorization checks can invalidate an old row. Do not
                # expose errors (possibly secret paths) or let it block unrelated work.
                continue
            try:
                if stopped():
                    return {'state':'stopped','execution_allowed':False}
                reply = self.adapter.reply(row['request_id'],self.transport)
            except (ValueError, OSError):
                reply = {'state':'blocked'}
            return {'admission':admitted,'reply':reply,'execution_allowed':False}
        if len(candidates) < 100:
            self._cursor = (-1, "")
        return {'admission':admitted,'reply':{'state':'idle'},'execution_allowed':False}
