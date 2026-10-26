"""Source-backed individual/organization identities for the current subject experiment."""
from __future__ import annotations
from collections import Counter
from experiments.maintenance_clock import maintenance_clock, maintenance_identity_scope
from copy import deepcopy
from typing import Literal
from pydantic import Field, model_validator
from remi.models import StrictModel, SourceCitation, SubjectRef, SubjectNameDraft, AliasEvidence
from remi.identity import normalized_name, source_name_present, stable_id
from remi.clock import parse_time

SubjectKind = Literal['individual', 'organization']

class DescriptionDraft(SourceCitation):
    text: str = Field(min_length=1, max_length=300)

class DescriptionEvidence(DescriptionDraft):
    quote: str
    observed_at: str

class IdentityReference(StrictModel):
    evidence_id: str = Field(description="Supplied C ID selecting an exact fragment of CURRENT SOURCE.")
    basis_id: str | None = Field(description="Supplied B basis owned by target_id for reuse; null for create/pending.")


class SubjectDraft(StrictModel):
    ref: SubjectRef = Field(description="One local integer ref per current identity, shared by all its names.")
    operation: Literal['create', 'reuse', 'pending'] = Field(description="Create a supported new identity, reuse a confirmed candidate, or retain a genuinely ambiguous mention as pending.")
    target_id: str | None = Field(description="Supplied E ID for reuse; null for create or pending.")
    name: str = Field(min_length=1, max_length=200,description="Display name or a specific literal referring phrase; @user identifies the API conversation user.")
    kind: SubjectKind = Field(description="individual for a particular person/animal; organization for a company/institution/team.")
    aliases: list[SubjectNameDraft] = Field(max_length=24,description="Literal names used in CURRENT SOURCE for this identity, including short names and nicknames; the program appends novel values.")
    descriptions: list[DescriptionDraft] = Field(max_length=24,description="New literal identifying descriptions of this subject, copied from CURRENT SOURCE; empty when unchanged.")
    reference: IdentityReference

class ReferenceEvidence(StrictModel):
    source_id: str
    quote: str = Field(min_length=1)
    observed_at: str


class SubjectRecord(StrictModel):
    id: str
    owner: str
    name: str
    kind: SubjectKind
    is_user: bool = False
    aliases: list[AliasEvidence] = Field(default_factory=list)
    descriptions: list[DescriptionEvidence] = Field(default_factory=list)
    references: list[ReferenceEvidence] = Field(default_factory=list)
    observed_at: str

    @model_validator(mode='after')
    def user_identity(self):
        canonical = self.id == stable_id('obj_', self.owner, 'user')
        if canonical != self.is_user or self.is_user and (self.name != 'user' or self.kind != 'individual'):
            raise ValueError('user_identity_requires_canonical_individual')
        return self


def visible_subject(raw, owner, clock):
    obj = SubjectRecord.model_validate(raw)
    if obj.owner != owner or parse_time(obj.observed_at) > parse_time(clock):
        return None
    return obj.model_copy(update={field: [row for row in getattr(obj, field)
        if parse_time(row.observed_at) <= parse_time(clock)] for field in ['aliases', 'descriptions', 'references']})


CONTEXT_LIMIT = 1
SUBJECT_LIMIT = 6


def keyword_score(label, kind, texts):
    """Name/alias matches precede descriptions; current words precede context within each kind."""
    word = normalized_name(label)
    hits = [index for index, text in enumerate(texts) if source_name_present(word, text)]
    if not hits:
        return (0, 0)
    return ((4 if 0 in hits else 3) if kind == 'name' else (2 if 0 in hits else 1), len(word))


def matching_subjects(store, owner, texts, clock, limit=SUBJECT_LIMIT):
    """Return keyword-ranked possible identities, with the canonical user first."""
    texts = [normalized_name(text) for text in ([*texts[:1], *texts[1:][-CONTEXT_LIMIT:]])]
    ranked = []
    for raw in store.list('objects', owner):
        obj = visible_subject(raw, owner, clock)
        if obj is None:
            continue
        labels = [(name, 'name') for name in [*([obj.name] if not obj.is_user else []), *[a.name for a in obj.aliases]]]
        labels.extend((d.text, 'description') for d in obj.descriptions)
        scores = [(keyword_score(label, kind, texts), label) for label, kind in labels]
        matches = list(dict.fromkeys(label for score, label in scores if score[0]))
        if matches or obj.is_user:
            ranked.append((max((score for score, _ in scores), default=(0, 0)), obj, matches))
    ranked.sort(key=lambda row: (-row[0][0], -row[0][1], row[1].id))
    users = [(obj, matches) for _, obj, matches in ranked if obj.is_user]
    others = [(obj, matches) for _, obj, matches in ranked if not obj.is_user]
    return (users + others)[:limit] if limit is not None else users + others


def recall_state(store, owner, source, context):
    """Use the shared keyword candidate builder in standalone subject validation."""
    from experiments.subject_recall import prepare_candidates, pack_history
    state, _ = prepare_candidates(store, dict(owner=owner, new_user_source=source, context_sources=context))
    selected = {obj['id'] for obj in state['subject_catalog']}
    history = [deepcopy(row) for row in store.list('memories', owner)
               if selected.intersection(row.get('entity_ids', []))]
    return pack_history(store, state, history)[0]


def resolve_subjects(raw, state, store):
    """Collect independent field failures, then materialize only validated identities and additions."""
    from pydantic import ValidationError
    from experiments.subject_validation import issue, schema_issues
    state['proposal'] = dict(subjects=[], items=[], reason='individual_subjects_with_descriptions')
    state['subject_alias_issues'] = []
    state['subject_description_issues'] = []
    rows = raw.get('subjects', [])
    counts = Counter(row.get('ref') for row in rows if isinstance(row, dict) and type(row.get('ref')) is int)
    targets = Counter(row.get('target_id') for row in rows if isinstance(row, dict) and row.get('operation') == 'reuse'
                      and isinstance(row.get('target_id'), str))
    catalog = {obj['id']: obj for obj in state['subject_catalog']}
    primary = state['new_user_source']; owner = state['owner']
    clock = maintenance_clock(state)
    from experiments.subject_references import current_evidence
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
                display=supplied_name if isinstance(supplied_name,str) and 0<len(supplied_name)<=200 else 'unresolved'
                supplied_kind=row.get('kind') if isinstance(row,dict) else None
                state['proposal']['subjects'].append(dict(ref=ref,name=display,
                    kind=supplied_kind if isinstance(supplied_kind,str) and supplied_kind in {'individual','organization'} else 'individual',
                    is_user=display=='@user' and primary['role']=='user',aliases=[],descriptions=[],evidence=[],**reference_fields))
        try:
            core = {**row, **{key: [] for key in ['aliases', 'descriptions'] if key in row}} if isinstance(row, dict) else row
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
                    emit(['target_id'],'speaker_marker_requires_canonical_individual','Select the supplied canonical conversation-user identity for @user.',
                         {'target_id':draft.target_id})
            basis = next((b for b in state.get('identity_bases',[]) if b['id']==draft.reference.basis_id),None)
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
                    field,label = (('references','quote') if basis['kind']=='reference' else
                                   ('aliases','name') if basis['kind']=='name' else ('descriptions','text'))
                    if not any(normalized_name(getattr(item,label))==normalized_name(basis['text']) and item.source_id==basis['source_id']
                               for item in getattr(old,field)):
                        emit(['reference','basis_id'],'reference_basis_requires_visible_owned_identity_evidence',
                             'Select visible name or description evidence stored on this identity.',{'basis_id':draft.reference.basis_id})
                    if basis_source and not present(basis['text'],[basis_source]):
                        emit(['reference','basis_id'],'reference_basis_requires_source_text','Use identity evidence grounded in its original source.',
                             {'basis_id':draft.reference.basis_id})
        else:
            if draft.reference.basis_id is not None:
                emit(['reference','basis_id'],'create_pending_use_null_basis','Use basis_id=null for create or pending.',
                     {'basis_id':draft.reference.basis_id},{'basis_id':None})
            if draft.target_id is not None:
                emit(['target_id'],'create_pending_use_null_target','Use target_id=null for create or pending.',
                     {'target_id':draft.target_id},{'target_id':None})
            if draft.operation=='create':
                key = stable_id('obj_',owner,'user') if marker else stable_id('obj_',owner,primary['id'],maintenance_identity_scope(state),ref)
                if store.get('objects',owner,key) is not None:
                    emit(['operation'],'existing_identity_requires_reuse','Reuse the existing identity from the supplied candidates.',
                         {'operation':draft.operation})
        is_user = old.is_user if old else marker
        if is_user and (draft.kind!='individual' or key not in {None,stable_id('obj_',owner,'user')}):
            emit(['kind'],'speaker_marker_requires_canonical_individual','Represent the actual conversation user as the canonical individual identity.',
                 expected={'kind':'individual'})
        additions = {}
        for field, label, model, scope in [('aliases', 'name', SubjectNameDraft, 'alias'),
                                         ('descriptions', 'text', DescriptionDraft, 'description')]:
            additions[field] = []; values = row.get(field)
            if not isinstance(values, list) or len(values) > 24:
                emit([field], field+'_require_bounded_list', 'Return an array containing at most 24 additions.', scope=scope)
                values = values[:24] if isinstance(values, list) else []
            for position, value in enumerate(values):
                try: parsed = model.model_validate(value)
                except ValidationError as error:
                    issues.extend(schema_issues(error, [*prefix, field, position], scope=scope, ref=ref, returned=value)); continue
                before = len(issues); source = support(parsed.source_id, [field, position, 'source_id'], scope)
                text = getattr(parsed, label)
                if parsed.source_id != primary['id']:
                    emit([field,position,'source_id'],field+'_require_current_source',"Attach this round's literal additions to CURRENT SOURCE.",
                         {'source_id':parsed.source_id},{'source_id':primary['id']},scope,[primary['id']])
                if source and not present(text, [source]):
                    emit([field, position, label], field+'_require_literal_source_text',
                         'Copy the name or description using wording present in its cited original.',
                         {label: text, 'source_id': parsed.source_id}, {'rule': 'Use a literal source span.'}, scope, [parsed.source_id])
                if source and len(issues) == before:
                    additions[field].append(dict(**parsed.model_dump(), quote=source['content'],
                        observed_at=clock))
        known_names = [old.name, *[a.name for a in old.aliases]] if old else []
        if old and not marker and normalized_name(draft.name) not in {
                normalized_name(n) for n in [*known_names, *[a['name'] for a in additions['aliases']]]}:
            emit(['name'], 'reuse_name_requires_selected_identity',
                 'Use a known name of the selected identity or a valid source-backed alias addition.',
                 {'name': draft.name}, {'names': known_names})
        if old is None and not marker and not present(draft.name, originals):
            emit(['name'], 'subject_identity_requires_source_anchor',
                 'Use a display name or unnamed referring phrase occurring in the cited original.',
                 {'name': draft.name}, {'rule': 'Copy the source referring phrase.'}, source_ids=[primary['id']])
        local = issues[start:]
        for entry in local:
            if entry['scope'] in {'alias', 'description'}:
                field = 'subject_alias_issues' if entry['scope'] == 'alias' else 'subject_description_issues'
                state[field].append(dict(ref=ref, index=entry['path'][3] if len(entry['path']) > 3 else None, error=entry['code']))
        if any(entry['scope'] == 'identity' for entry in local):
            decisions[ref] = dict(id=None, object=None)
            unresolved()
            continue
        if key is not None:
            current = store.get('objects', owner, key)
            obj = SubjectRecord.model_validate(current) if current else SubjectRecord(
                id=key, owner=owner, name='user' if is_user else draft.name, kind=draft.kind, is_user=is_user, observed_at=clock)
            obj.observed_at = min([obj.observed_at, clock], key=parse_time)
            if current_valid and not any(item.source_id==primary['id'] and item.quote==fragment['text'] for item in obj.references):
                obj.references.append(ReferenceEvidence(source_id=primary['id'],quote=fragment['text'],observed_at=clock))
            for field, label, model in [('aliases', 'name', AliasEvidence), ('descriptions', 'text', DescriptionEvidence)]:
                evidence = getattr(obj, field); seen = {(normalized_name(getattr(item, label)), item.source_id) for item in evidence}
                for value in additions[field]:
                    identity = (normalized_name(value[label]), value['source_id'])
                    if identity not in seen:
                        evidence.append(model.model_validate(value)); seen.add(identity)
            decisions[ref] = dict(id=key, object=obj.model_dump())
        else: decisions[ref] = dict(id=None, object=None)
        state['proposal']['subjects'].append(dict(ref=ref, name=draft.name, kind=draft.kind, is_user=is_user,
            aliases=list(dict.fromkeys([*known_names, *[a['name'] for a in additions['aliases']]])),
            descriptions=[d['text'] for d in additions['descriptions']],
            evidence=[dict(source_id=primary['id'],quote=fragment['text'])] if current_valid else [],**reference_fields))
    return dict(subject_ids={str(ref): row['id'] for ref, row in decisions.items() if row['id'] is not None},
                entity_rows=[row['object'] for row in decisions.values() if row['object'] is not None]), issues
