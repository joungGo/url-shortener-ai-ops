"""Authenticated event intake adapter; notice delivery remains independently unknown."""
from ai_ops.event_store import EventStore


class EventInbox:
    """One token/TLS endpoint is bound to one server-configured logical source.

    Alert payloads and HTTP headers cannot choose a different source. This adapter
    commits event metadata only: it never drains jobs or sends Slack messages.
    """
    def __init__(self, store: EventStore, *, source_alias: str):
        if source_alias not in store.sources:
            raise ValueError('unregistered source alias')
        self.store = store
        self.source_alias = source_alias

    def accept(self, raw: bytes) -> dict:
        events = self.store.accept(self.source_alias, raw)
        return {'accepted':len(events),'rejected':0,'event_ids':list(dict.fromkeys(events)),
                'notification':'unknown','investigation_started':False,'execution_allowed':False}
