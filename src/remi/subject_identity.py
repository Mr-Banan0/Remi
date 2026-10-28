"""Source-backed people, animals and organizations for the current subject experiment."""
from __future__ import annotations
from collections import Counter
from .maintenance_clock import maintenance_clock, maintenance_identity_scope
from copy import deepcopy
from typing import Literal
from pydantic import Field, model_validator
from .models import StrictModel, SubjectRef, SubjectNameDraft, AliasEvidence, SCHEMA_VERSION
from .identity import normalized_name, source_name_present, stable_id
from .clock import parse_time

SubjectKind = Literal['person', 'animal', 'organization']

class IdentityReference(StrictModel):
    evidence_id: str = Field(description="Supplied C ID selecting an exact fragment of CURRENT SOURCE.")
    basis_id: str | None = Field(description="Supplied B basis owned by target_id for reuse; null for create/pending.")


class SubjectDraft(StrictModel):
    ref: SubjectRef = Field(description="One local integer ref per current identity, shared by all its names.")
    operation: Literal['create', 'reuse', 'pending'] = Field(description="Create a supported new identity, reuse a confirmed candidate, or retain a genuinely ambiguous mention as pending.")
    target_id: str | None = Field(description="Supplied E ID for reuse; null for create or pending.")
    name: str | None = Field(description="First confirmed name; null while the subject has no stated name. @user identifies the API conversation user.")
    kind: SubjectKind = Field(description="person, animal, or organization.")
    aliases: list[SubjectNameDraft] = Field(max_length=24,description="Only new proper names, nicknames, abbreviations or renamed organization names stated in CURRENT SOURCE. Exclude occupations, roles, relationships, demographic attributes, animal categories and descriptive phrases.")
    reference: IdentityReference

class ReferenceEvidence(StrictModel):
    source_id: str
    quote: str = Field(min_length=1)
    observed_at: str


class SubjectRecord(StrictModel):
    schema_version: Literal[13] = SCHEMA_VERSION
    id: str
    owner: str
    name: str | None
    kind: SubjectKind
    is_user: bool = False
    aliases: list[AliasEvidence] = Field(default_factory=list)
    references: list[ReferenceEvidence] = Field(default_factory=list)
    observed_at: str

    @model_validator(mode='after')
    def user_identity(self):
        canonical = self.id == stable_id('obj_', self.owner, 'user')
        if canonical != self.is_user or self.is_user and (self.name != 'user' or self.kind != 'person'):
            raise ValueError('user_identity_requires_canonical_person')
        return self


def visible_subject(raw, owner, clock):
    obj = SubjectRecord.model_validate(raw)
    if obj.owner != owner or parse_time(obj.observed_at) > parse_time(clock):
        return None
    return obj.model_copy(update={field: [row for row in getattr(obj, field)
        if parse_time(row.observed_at) <= parse_time(clock)] for field in ['aliases', 'references']})


CONTEXT_LIMIT = 1
SUBJECT_LIMIT = 6


def keyword_score(label, texts):
    """Rank exact subject names and aliases found in the current source or context."""
    word = normalized_name(label)
    hits = [index for index, text in enumerate(texts) if source_name_present(word, text)]
    if not hits:
        return (0, 0)
    return (4 if 0 in hits else 3, len(word))


def matching_subjects(store, owner, texts, clock, limit=SUBJECT_LIMIT):
    """Return keyword-ranked possible identities, with the canonical user first."""
    texts = [normalized_name(text) for text in ([*texts[:1], *texts[1:][-CONTEXT_LIMIT:]])]
    ranked = []
    for raw in store.list('objects', owner):
        obj = visible_subject(raw, owner, clock)
        if obj is None:
            continue
        labels = [*([obj.name] if obj.name and not obj.is_user else []), *[a.name for a in obj.aliases]]
        scores = [(keyword_score(label, texts), label) for label in labels]
        matches = list(dict.fromkeys(label for score, label in scores if score[0]))
        if matches or obj.is_user:
            ranked.append((max((score for score, _ in scores), default=(0, 0)), obj, matches))
    ranked.sort(key=lambda row: (-row[0][0], -row[0][1], row[1].id))
    users = [(obj, matches) for _, obj, matches in ranked if obj.is_user]
    others = [(obj, matches) for _, obj, matches in ranked if not obj.is_user]
    return (users + others)[:limit] if limit is not None else users + others


def recall_state(store, owner, source, context):
    """Use the shared keyword candidate builder in standalone subject validation."""
    from .subject_recall import prepare_candidates, pack_history
    state, _ = prepare_candidates(store, dict(owner=owner, new_user_source=source, context_sources=context))
    selected = {obj['id'] for obj in state['subject_catalog']}
    history = [deepcopy(row) for row in store.list('memories', owner)
               if selected.intersection(row.get('entity_ids', []))]
    return pack_history(store, state, history)[0]


def resolve_subjects(raw, state, store):
    """Collect independent field failures, then materialize only validated identities and additions."""
    from pydantic import ValidationError
    from .subject_validation import issue, schema_issues
    state['proposal'] = dict(subjects=[], items=[], reason='named_subjects')
    state['subject_alias_issues'] = []
    rows = raw.get('subjects', [])
    counts = Counter(row.get('ref') for row in rows if isinstance(row, dict) and type(row.get('ref')) is int)
    targets = Counter(row.get('target_id') for row in rows if isinstance(row, dict) and row.get('operation') == 'reuse'
                      and isinstance(row.get('target_id'), str))
    catalog = {obj['id']: obj for obj in state['subject_catalog']}
    primary = state['new_user_source']; owner = state['owner']
    clock = maintenance_clock(state)
    from .subject_references import current_evidence
    fragments = {row['id']: row for row in current_evidence(primary)}
    state['current_evidence'] = list(fragments.values())
    decisions, issues = {}, []

    def present(text, originals):
        return any(source_name_present(normalized_name(text), normalized_name(s['content'])) for s in originals)

    for index, row in enumerate(rows[:96]):
        ref = row.get('ref', -index-1) if isinstance(row, dict) else -index-1
        if type(ref) is not int: ref = -index-1
        start = len(issues); prefix = ['subjects', index]

        def emit(field, code, message, actual=None, expected=None, scope='identity', source_ids=()):
            issues.append(issue(scope, [*prefix, *field], code, message, ref=ref,
                                actual=actual, expected=expected, source_ids=source_ids))

        def support(sid, field, scope='identity'):
            source = state['source_catalog'].get(sid)
            if source is None or source.get('owner', owner) != owner:
                emit(field, 'subject_source_requires_owned_supplied_original',
                     'Choose a supplied original source belonging to this user.', {'source_id': sid},
                     {'source_ids': list(state['source_catalog'])}, scope)
                return None
            try: visible = parse_time(source.get('message_at') or source.get('created_at') or clock) <= parse_time(clock)
            except (ValueError, TypeError): visible = False
            if not visible:
                emit(field, 'subject_source_requires_visible_original',
                     'Choose an original visible at the current source time.', {'source_id': sid},
                     {'source_id': primary['id']}, scope)
                return None
            return source

        supplied_reference = row.get('reference') if isinstance(row,dict) else None
        evidence_id=supplied_reference.get('evidence_id') if isinstance(supplied_reference,dict) else None
        fragment=fragments.get(evidence_id) if isinstance(evidence_id,str) else None
        def current_provenance_valid():
            supplied = state['source_catalog'].get(primary['id'])
            if supplied is None:return False
            stored = store.get('sources',owner,primary['id'])
            for original in [primary,supplied,*([stored] if stored is not None else [])]:
                if original.get('owner',owner)!=owner or original.get('id')!=primary['id']:
                    return False
                if original.get('role')!=primary['role']:return False
                try:
                    if parse_time(original.get('message_at') or original.get('created_at') or clock)>parse_time(clock):return False
                except (ValueError,TypeError):return False
                text=original.get('content')
                if text==primary['content']:continue
                window=state.get('source_window',{})
                start,stop=window.get('start'),window.get('stop')
                if (not isinstance(text,str) or type(start) is not int or type(stop) is not int
                        or not 0<=start<=stop<=len(text) or text[start:stop]!=primary['content']):return False
            return True
        current_valid = bool(fragment) and current_provenance_valid()
        if fragment is not None and not current_valid:
            emit(['reference','evidence_id'],'reference_current_provenance_mismatch',
                 'Select C evidence from the owned current original with its source speaker, time and exact source window.',
                 {'evidence_id':fragment['id']},{'source_id':primary['id']},source_ids=[primary['id']])
        reference_fields = dict(current_reference_valid=current_valid,
                                current_evidence_id=fragment['id'] if current_valid else None)
        def unresolved():
            if not any(s['ref'] == ref for s in state['proposal']['subjects']):
                supplied_name=row.get('name') if isinstance(row,dict) else None
                display=supplied_name if isinstance(supplied_name,str) and 0<len(supplied_name)<=200 else None
                supplied_kind=row.get('kind') if isinstance(row,dict) else None
                state['proposal']['subjects'].append(dict(ref=ref,name=display,
                    kind=supplied_kind if isinstance(supplied_kind,str) and supplied_kind in {'person','animal','organization'} else 'person',
                    is_user=display=='@user' and primary['role']=='user',aliases=[],evidence=[],**reference_fields))
        try:
            core = {**row, **({'aliases': []} if 'aliases' in row else {})} if isinstance(row, dict) else row
            draft = SubjectDraft.model_validate(core)
        except ValidationError as error:
            issues.extend(schema_issues(error,prefix,ref=ref,returned=row))
            if 0 <= ref <= 95:
                decisions[ref] = dict(id=None,object=None); unresolved()
            continue
        if counts[ref] != 1:
            emit(['ref'],'subject_ref_selected_more_than_once','Assign one row to each local subject ref.',
                 {'ref':ref},{'rule':'Each ref appears once.'})
        if fragment is None:
            emit(['reference','evidence_id'],'reference_requires_current_evidence',
                 'Select a supplied C fragment of CURRENT SOURCE.',{'evidence_id':draft.reference.evidence_id},
                 {'evidence_ids':list(fragments)},source_ids=[primary['id']])
        originals = [primary] if current_valid else []
        marker = draft.name == '@user'; old = None; key = None
        if marker and primary['role'] != 'user':
            emit(['reference'],'speaker_identity_requires_user_source','Use the actual conversation user only for a user source.',
                 expected={'source_id':primary['id']},source_ids=[primary['id']])
        if draft.operation == 'reuse':
            stored = store.get('objects',owner,draft.target_id) if draft.target_id in catalog else None
            try: old = visible_subject(stored,owner,clock) if stored else None
            except (ValueError,TypeError): old = None
            if old is None:
                emit(['target_id'],'reuse_requires_visible_owned_subject','Select a supplied, visible subject ID owned by this user.',
                     {'target_id':draft.target_id},{'entity_ids':list(catalog)})
            else:
                key = old.id
                if targets[draft.target_id] != 1:
                    emit(['target_id'],'existing_identity_uses_one_local_ref','Represent this stored identity with one local ref.',
                         {'target_id':draft.target_id},{'rule':'One local ref per reused ID.'})
                if old.kind != draft.kind:
                    emit(['kind'],'reuse_requires_same_subject_kind','Use the stored subject kind for this ID.',
                         {'kind':draft.kind},{'kind':old.kind})
                if marker and not old.is_user:
                    emit(['target_id'],'speaker_marker_requires_canonical_person','Select the supplied canonical conversation-user identity for @user.',
                         {'target_id':draft.target_id})
            basis_id=draft.reference.basis_id
            if basis_id is None and old is not None:
                current_literals={normalized_name(group.get('name','')) for group in
                                  state.get('maintenance_route',{}).get('name_matches',[])
                                  if draft.target_id in group.get('subject_ids',[])}
                inferred=[b for b in state.get('identity_bases',[]) if b['target_id']==draft.target_id
                          and b['kind']=='name' and normalized_name(b['text']) in current_literals]
                if len(inferred)==1:basis_id=inferred[0]['id']
            basis = next((b for b in state.get('identity_bases',[]) if b['id']==basis_id),None)
            if basis is None:
                emit(['reference','basis_id'],'reference_requires_supplied_basis','Choose a supplied B identity basis belonging to the selected target_id.',
                     {'basis_id':draft.reference.basis_id},{'target_id':draft.target_id})
            elif basis['target_id'] != draft.target_id:
                emit(['reference','basis_id'],'reference_basis_belongs_to_other_subject','Select a basis owned by the same target_id.',
                     {'basis_id':draft.reference.basis_id,'target_id':basis['target_id']},{'target_id':draft.target_id})
            elif old:
                if basis['kind']=='canonical_user':
                    if not old.is_user or not marker:
                        emit(['reference','basis_id'],'canonical_basis_requires_user_marker','Use the canonical-user basis with @user.')
                else:
                    basis_source = support(basis['source_id'],['reference','basis_id'])
                    if basis['kind']=='reference':
                        valid_basis=any(normalized_name(item.quote)==normalized_name(basis['text']) and item.source_id==basis['source_id']
                                        for item in old.references)
                    else:
                        valid_alias=any(normalized_name(item.name)==normalized_name(basis['text']) and item.source_id==basis['source_id']
                                        for item in old.aliases)
                        valid_canonical=bool(old.name and normalized_name(old.name)==normalized_name(basis['text']) and
                            any(item.source_id==basis['source_id'] and source_name_present(normalized_name(old.name),normalized_name(item.quote))
                                for item in old.references))
                        valid_basis=valid_alias or valid_canonical
                    if not valid_basis:
                        emit(['reference','basis_id'],'reference_basis_requires_visible_owned_identity_evidence',
                             'Select visible name, alias or reference evidence stored on this identity.',{'basis_id':draft.reference.basis_id})
                    if basis_source and not present(basis['text'],[basis_source]):
                        emit(['reference','basis_id'],'reference_basis_requires_source_text','Use identity evidence grounded in its original source.',
                             {'basis_id':draft.reference.basis_id})
            route=state.get('maintenance_route',{})
            collision=next((group for group in route.get('collision_groups',[])
                            if draft.target_id in group.get('subject_ids',[])),None)
            unique_literal=any(group.get('subject_ids')==[draft.target_id]
                               for group in route.get('name_matches',[]))
            compatible=[] if collision is None else [subject_id for subject_id in collision['subject_ids']
                if catalog.get(subject_id,{}).get('kind')==draft.kind]
            if old and collision and len(compatible)>1 and not unique_literal:
                emit(['target_id'],'reuse_requires_unique_identity_evidence',
                     'The current name matches multiple stored identities of this kind. Use pending when the current source and context do not uniquely identify one; use create for an explicitly distinct new identity.',
                     {'target_id':draft.target_id,'matched_ids':compatible,'matched_name':collision['name']},
                     {'operations':['pending','create'],'rule':'reuse requires a unique current name, alias, or compatible identity'},
                     source_ids=[primary['id']])
        else:
            if draft.reference.basis_id is not None:
                emit(['reference','basis_id'],'create_pending_use_null_basis','Use basis_id=null for create or pending.',
                     {'basis_id':draft.reference.basis_id},{'basis_id':None})
            if draft.target_id is not None:
                emit(['target_id'],'create_pending_use_null_target','Use target_id=null for create or pending.',
                     {'target_id':draft.target_id},{'target_id':None})
            if draft.operation=='create':
                key = stable_id('obj_',owner,'user') if marker else stable_id('obj_',owner,primary['id'],maintenance_identity_scope(state),ref)
                if draft.name is None:
                    emit(['name'],'create_requires_stated_name',
                         'Create a subject only when CURRENT SOURCE states its proper name. Keep unnamed people and animals in memory content.',
                         {'name':None},{'rule':'Use a stated proper name or withdraw this subject.'},source_ids=[primary['id']])
                    key=None
                if key is not None and store.get('objects',owner,key) is not None:
                    emit(['operation'],'existing_identity_requires_reuse','Reuse the existing identity from the supplied candidates.',
                         {'operation':draft.operation})
                route=state.get('maintenance_route',{})
                label=normalized_name(draft.name or '')
                matching=[]
                for group in route.get('name_matches',[]):
                    if normalized_name(group.get('name','')) != label:
                        continue
                    compatible=[subject_id for subject_id in group.get('subject_ids',[])
                                if catalog.get(subject_id,{}).get('kind')==draft.kind]
                    if compatible:matching.extend(compatible)
                alias_hit=any(label in {normalized_name(alias.get('name','')) for alias in
                                        catalog.get(subject_id,{}).get('aliases',[])}
                              for subject_id in matching)
                canonical_alias_update=(isinstance(row.get('aliases'),list) and bool(row['aliases']) and any(
                    normalized_name(catalog.get(subject_id,{}).get('name') or '')==label
                    for subject_id in matching))
                confirmed=any(entry.get('code')=='create_overlaps_recalled_identity'
                              and entry.get('ref')==ref for entry in state.get('repair_errors',[]))
                if matching and (alias_hit or canonical_alias_update) and not confirmed:
                    emit(['operation'],'create_overlaps_recalled_identity',
                         'The current name or alias matches a recalled identity of this kind. Select reuse for one uniquely established identity, pending for an unresolved collision, or repeat create in the repair response when CURRENT SOURCE explicitly establishes a distinct new identity.',
                         {'operation':'create','name':draft.name,'matched_ids':list(dict.fromkeys(matching))},
                         {'operations':['reuse','pending','create_confirmation']},source_ids=[primary['id']])
            elif draft.operation=='pending' and draft.name is not None:
                route=state.get('maintenance_route',{})
                label=normalized_name(draft.name)
                relevant=[]
                for group in route.get('name_matches',[]):
                    group_ids=group.get('subject_ids',[])
                    group_names={normalized_name(group.get('name',''))}
                    group_names.update(normalized_name(catalog[sid].get('name') or '') for sid in group_ids if sid in catalog)
                    if label in group_names:relevant.append(group)
                maximum=max((len(group.get('subject_ids',[])) for group in relevant),default=0)
                if maximum<2:
                    emit(['operation'],'pending_requires_multiple_identity_candidates',
                         'A named pending subject requires at least two matching stored identities. Use create for a new identity or reuse for the uniquely matched identity.',
                         {'operation':'pending','name':draft.name,'matching_candidate_count':maximum},
                         {'operations':['create','reuse']},source_ids=[primary['id']])
        is_user = old.is_user if old else marker
        if is_user and (draft.kind!='person' or key not in {None,stable_id('obj_',owner,'user')}):
            emit(['kind'],'speaker_marker_requires_canonical_person','Represent the actual conversation user as the canonical person identity.',
                 expected={'kind':'person'})
        additions = {'aliases': []}; values = row.get('aliases')
        if not isinstance(values, list) or len(values) > 24:
            emit(['aliases'], 'aliases_require_bounded_list', 'Return an array containing at most 24 additions.', scope='alias')
            values = values[:24] if isinstance(values, list) else []
        for position, value in enumerate(values):
            try: parsed = SubjectNameDraft.model_validate(value)
            except ValidationError as error:
                issues.extend(schema_issues(error, [*prefix, 'aliases', position], scope='alias', ref=ref, returned=value)); continue
            before = len(issues); source = support(parsed.source_id, ['aliases', position, 'source_id'], 'alias')
            if parsed.source_id != primary['id']:
                emit(['aliases',position,'source_id'],'aliases_require_current_source',"Attach this round's literal additions to CURRENT SOURCE.",
                     {'source_id':parsed.source_id},{'source_id':primary['id']},'alias',[primary['id']])
            if source and not present(parsed.name, [source]):
                emit(['aliases', position, 'name'], 'aliases_require_literal_source_text',
                     'Copy the alias using wording present in its cited original.',
                     {'name': parsed.name, 'source_id': parsed.source_id}, {'rule': 'Use a literal source span.'}, 'alias', [parsed.source_id])
            if source and len(issues) == before:
                additions['aliases'].append(dict(**parsed.model_dump(), quote=source['content'], observed_at=clock))
        known_names = [n for n in ([old.name, *[a.name for a in old.aliases]] if old else []) if n]
        returned_names = {*[normalized_name(n) for n in known_names],
                          *[normalized_name(a['name']) for a in additions['aliases']]}
        if old and old.name is not None and not marker and normalized_name(draft.name or '') != normalized_name(old.name):
            emit(['name'], 'reuse_name_requires_selected_identity',
                 'Keep the first confirmed name of the selected identity.',
                 {'name': draft.name}, {'name': old.name})
        if draft.name is not None and old is not None and old.name is None and not present(draft.name, originals):
            emit(['name'],'first_name_requires_current_source','Set the first confirmed name from CURRENT SOURCE.',
                 {'name':draft.name},{'rule':'Copy the stated name.'},source_ids=[primary['id']])
        if old is None and draft.name is not None and not marker and not present(draft.name, originals):
            emit(['name'], 'subject_identity_requires_source_anchor',
                 'Use the first stated name occurring in CURRENT SOURCE, or null for an unnamed subject.',
                 {'name': draft.name}, {'rule': 'Copy the stated name.'}, source_ids=[primary['id']])
        if draft.name is None and additions['aliases']:
            emit(['aliases'], 'unnamed_subject_cannot_have_aliases',
                 'Use the first stated name as name before storing later alternative names.',
                 {'aliases':[value['name'] for value in additions['aliases']]},
                 {'name':'first confirmed name','aliases':[]})
        existing_labels={normalized_name(n) for n in [*known_names,*([draft.name] if draft.name else [])]}
        additions['aliases']=[value for value in additions['aliases'] if normalized_name(value['name']) not in existing_labels]
        local = issues[start:]
        for entry in local:
            if entry['scope'] == 'alias':
                state['subject_alias_issues'].append(dict(ref=ref, index=entry['path'][3] if len(entry['path']) > 3 else None, error=entry['code']))
        if any(entry['scope'] == 'identity' for entry in local):
            decisions[ref] = dict(id=None, object=None)
            unresolved()
            continue
        if key is not None:
            current = store.get('objects', owner, key)
            obj = SubjectRecord.model_validate(current) if current else SubjectRecord(
                id=key, owner=owner, name='user' if is_user else draft.name, kind=draft.kind,
                is_user=is_user, observed_at=clock)
            if obj.name is None and draft.name is not None:
                obj.name=draft.name
            obj.observed_at = min([obj.observed_at, clock], key=parse_time)
            if current_valid and not any(item.source_id==primary['id'] and item.quote==fragment['text'] for item in obj.references):
                obj.references.append(ReferenceEvidence(source_id=primary['id'],quote=fragment['text'],observed_at=clock))
            evidence = obj.aliases; seen = {normalized_name(item.name) for item in evidence}
            for value in additions['aliases']:
                identity = normalized_name(value['name'])
                if identity not in seen:
                    evidence.append(AliasEvidence.model_validate(value)); seen.add(identity)
            decisions[ref] = dict(id=key, object=obj.model_dump())
        else: decisions[ref] = dict(id=None, object=None)
        final_name=('@user' if is_user else (old.name if old and old.name is not None else draft.name))
        final_aliases=([a.name for a in old.aliases] if old else [])+ [a['name'] for a in additions['aliases']]
        state['proposal']['subjects'].append(dict(ref=ref, name=final_name, kind=draft.kind,
            is_user=is_user,aliases=list(dict.fromkeys(final_aliases)),
            evidence=[dict(source_id=primary['id'],quote=fragment['text'])] if current_valid else [],**reference_fields))
    return dict(subject_ids={str(ref): row['id'] for ref, row in decisions.items() if row['id'] is not None},
                entity_rows=[row['object'] for row in decisions.values() if row['object'] is not None]), issues
