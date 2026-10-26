"""Build request-local references to owned, visible identity evidence."""
from experiments.maintenance_clock import maintenance_clock
from copy import deepcopy
import re
from remi.clock import parse_time
from remi.identity import normalized_name,source_name_present,stable_id


def current_evidence(source):
    """Number exact sentence/clause spans in the current original, in source order."""
    text = source['content']; rows = []
    for match in re.finditer(r'[^.!?。！？;；\n]+(?:[.!?。！？;；\n]+|$)', text):
        start, stop = match.span()
        while start < stop and text[start].isspace(): start += 1
        while stop > start and text[stop-1].isspace(): stop -= 1
        if start < stop:
            rows.append(dict(id='C'+str(len(rows)+1), source_id=source['id'], start=start,
                             stop=stop, text=text[start:stop]))
    if not rows and text:
        start = 0; stop = len(text)
        rows.append(dict(id='C1',source_id=source['id'],start=start,stop=stop,text=text[start:stop]))
    return rows


def prepare_reference_state(state,store):
    state=deepcopy(state);owner=state['owner'];primary=state['new_user_source']
    clock=maintenance_clock(state);bases=[]
    sources=state.setdefault('source_catalog',{s['id']:s for s in [primary,*state.get('context_sources',[])]})
    for subject in state.get('subject_catalog',[]):
        current=store.get('objects',owner,subject['id'])
        if current is None:continue
        if current.get('owner')!=owner or parse_time(current['observed_at'])>parse_time(clock):continue
        def add(kind,text,sid=None):
            bases.append(dict(id='B'+str(len(bases)+1),target_id=subject['id'],kind=kind,text=text,source_id=sid))
        if current.get('is_user'):
            if current['id']==stable_id('obj_',owner,'user'):add('canonical_user','the current conversation user')
        eligible={'name':[], 'reference':[]}
        seen=set()
        if current.get('name') and not current.get('is_user'):
            key=normalized_name(current['name'])
            for item in current.get('references',[]):
                if parse_time(item['observed_at']) > parse_time(clock):continue
                sid=item['source_id'];original=store.get('sources',owner,sid) or sources.get(sid)
                if original is None or original.get('owner',owner)!=owner:continue
                if parse_time(original.get('message_at') or original['created_at']) > parse_time(clock):continue
                if item['quote'] in original['content'] and source_name_present(key,normalized_name(item['quote'])):
                    seen.add(key);eligible['name'].append(('name',current['name'],sid,original));break
        for supplied in subject.get('aliases',[]):
            key=normalized_name(supplied['name'])
            if key in seen:continue
            item=next((row for row in current.get('aliases',[]) if normalized_name(row['name'])==key
                       and row['source_id']==supplied['source_id']),None)
            if item is None or parse_time(item['observed_at'])>parse_time(clock):continue
            sid=item['source_id'];original=store.get('sources',owner,sid) or sources.get(sid)
            if original is None or original.get('owner',owner)!=owner:continue
            if parse_time(original.get('message_at') or original['created_at'])>parse_time(clock):continue
            text=item['name'];quote=item.get('quote','')
            if not quote or quote not in original['content']:continue
            if not source_name_present(normalized_name(text),normalized_name(quote)):continue
            seen.add(key);eligible['name'].append(('name',text,sid,original))
        for item in current.get('references',[]):
            if parse_time(item['observed_at']) > parse_time(clock):continue
            sid=item['source_id'];original=store.get('sources',owner,sid) or sources.get(sid)
            if original is None or original.get('owner',owner)!=owner:continue
            if parse_time(original.get('message_at') or original['created_at']) > parse_time(clock):continue
            if item['quote'] and item['quote'] in original['content']:
                eligible['reference'].append(('reference',item['quote'],sid,original))
        # Current-reference evidence also supports unnamed identities independently of aliases.
        selected=[*eligible['name'][:2]]
        if len(selected)<2:selected.extend(eligible['reference'][:2-len(selected)])
        for kind,text,sid,original in selected:
            sources[sid]=original;add(kind,text,sid)
    state['current_evidence']=current_evidence(primary)
    state['identity_bases']=bases
    return state
