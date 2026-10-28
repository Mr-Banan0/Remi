"""Keep knowledge visibility separate from the original relative-date anchor."""
def maintenance_clock(state):
    source = state['new_user_source']
    return state.get('recovery_clock') or source.get('message_at') or source['created_at']


def maintenance_identity_scope(state):
    scope = state.get('source_window')
    if state.get('recovery'):
        return dict(source_window=scope, pending_key=state['recovery']['key'])
    return scope
